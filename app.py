import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
import pickle

import streamlit as st
from langchain.chains import RetrievalQA
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_community.llms import HuggingFaceHub
from langchain_community.document_loaders import DirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

from langchain_core.documents import Document
from langchain_openai import ChatOpenAI

# Defining paths 
os.environ["TRANSFORMERS_OFFLINE"] = "0"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["TOKENIZERS_PARALLELISM"] = "false"


TMP_DIR = Path(__file__).resolve().parent.joinpath('tmp')
LOCAL_VECTOR_STORE_DIR = Path(__file__).resolve().parent.joinpath('vector_store')
EMBEDDINGS_DIR = Path(__file__).resolve().parent.joinpath('embeddings')

TMP_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)

# Define namespaces for XML-tei
NAMESPACES = {
    'tei': 'http://www.tei-c.org/ns/1.0'
}

st.set_page_config(page_title="RAG Démonstration", page_icon="🤖", layout="wide")
st.title("Retrieval Augmented Generation")
if os.path.exists("static/sfp_logo.png"):
    st.image("static/sfp_logo.png", width=100)
st.markdown("#### Projet préparé par l'équipe ObTIC.")

# Fixed system prompt - not modifiable by users
SYSTEM_PROMPT = """
Tu es ChatSFP, un assistant développé par la Société Française de Parasitologie pour valoriser les bulletins de la SFP de manière interactive.

Ton rôle est de répondre aux questions des utilisateurs en t’appuyant exclusivement sur les documents fournis, qui sont extraits automatiquement à partir de leur requête.

Cependant, certaines requêtes ne sont pas pertinentes pour ChatSFP. Voici comment tu dois les traiter :

1. Si la requête concerne le fonctionnement de l’assistant lui-même (par exemple : "Qui es-tu ?", "Quel est ton rôle ?"), tu peux répondre directement sans utiliser les documents.

2. Si la requête est générale, hors sujet, ou ne concerne pas le contenu scientifique des bulletins (exemples : "Comment vas-tu ?", "Traduis ce mot", "Quelle est la capitale de la France ?", ou une phrase sans sens), indique poliment que tu ne peux pas répondre à cette demande dans le cadre de ta mission.

3. Si la requête est en lien avec la parasitologie ou les sujets couverts dans les bulletins, tu dois impérativement fonder ta réponse sur les documents fournis.

IMPORTANT : Pour chaque information issue des documents, tu dois mentionner explicitement la source correspondante (par exemple : Source 1, Source 2, etc.).
"""

# Default query prompt - can be modified by users
DEFAULT_QUERY_PROMPT = """Voici la requête de l'utilisateur :  
{query}

# Instructions COSTAR pour traiter cette requête :

[C] **Contexte** : Documents scientifiques historiques en français, au format XML-TEI. Corpus vectorisé disponible. Présence fréquente d'erreurs OCR, notamment sur les chiffres. Entrée = question + documents pertinents.

[O] **Objectif** : Fournir des réponses factuelles et précises, exclusivement basées sur les documents fournis. L'extraction doit être claire, structurée, et signaler toute erreur OCR détectée. Ne rien inventer.

[S] **Style** : Clair et structuré. Utiliser le Markdown pour marquer la hiérarchie. Séparer les faits établis des incertitudes. Citer les documents avec exactitude.

[T] **Ton** : Professionnel et académique. Précis, neutre, et transparent quant aux limites des réponses.

[A] **Audience** : Chercheurs et historien·ne·s, en quête d'informations fiables, vérifiables et bien sourcées.

[R] **Réponse** :  
- Titres en **gras** - Informations citées textuellement depuis les documents  
- Pour chaque information importante, indiquer explicitement le numéro de la source (ex: Source 1, Source 2, etc.)
- En l'absence d'information : écrire _"Les documents fournis ne contiennent pas cette information."_  
- Chaque information doit comporter un **niveau de confiance** : Élevé / Moyen / Faible  
- Chiffres présentés de manière claire et lisible  
- Mettre en **gras** les informations importantes
- 4-5 phrases maximum

⚠️ **Attention aux chiffres** : les erreurs OCR sont fréquentes. Vérifier la cohérence à partir du contexte. Être prudent sur les séparateurs utilisés (espaces, virgules, points)."""

def extract_year(date_str):
    """Extract year from a date string."""
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', date_str)
    if year_match:
        return int(year_match.group(1))
    return None

def parse_xmltei_document(file_path):
    """Parse an XML-TEI document and extract text content with metadata."""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        # Extract document metadata for context
        title = root.find('.//tei:titleStmt/tei:title', NAMESPACES)
        title_text = title.text if title is not None else "Unknown Title"
        
        # Extract publication date
        date = root.find('.//tei:sourceDesc/tei:p/tei:date', NAMESPACES)
        if date is None:
            date = root.find('.//tei:sourceDesc/tei:p', NAMESPACES)
        date_text = date.text if date is not None else "Unknown Date"
        
        # Extract year
        year = extract_year(date_text)
        
        # Get all paragraphs
        paragraphs = root.findall('.//tei:p', NAMESPACES)
        
        # Also get all persName elements to find scientists/authors
        person_names = root.findall('.//tei:persName', NAMESPACES)
        person_text = []
        for person in person_names:
            name = ''.join(person.itertext()).strip()
            if name:
                person_text.append(name)
        
        # Create document header with metadata
        header = f"Document: {title_text} | Date: {date_text}\n\n"
        
        # Extract paragraph text
        all_paragraphs = []
        for para in paragraphs:
            para_text = ''.join(para.itertext()).strip()
            if para_text:
                all_paragraphs.append(para_text)
        
        # Combine header with paragraphs
        full_text = header + "\n".join(all_paragraphs)
        
        if person_text:
            person_section = "\n\nPersonnes mentionnées: " + ", ".join(person_text)
            full_text += person_section
        
        return {
            "title": title_text,
            "date": date_text,
            "year": year,
            "text": full_text,
            "persons": person_text
        }
        
    except Exception as e:
        st.error(f"Error parsing XML file {file_path}: {str(e)}")
        return None

def load_documents(use_uploaded_only=False):
    """Load XML-TEI documents"""
    documents = []
    document_dates = {}
    xml_files = []
    
    if use_uploaded_only:
        if "uploaded_files" in st.session_state and st.session_state.uploaded_files:
            for file_path in st.session_state.uploaded_files:
                if os.path.exists(file_path) and (file_path.endswith(".xml") or file_path.endswith(".xmltei")):
                    xml_files.append(file_path)
    else:
        for path in [".", "data"]:
            if os.path.exists(path):
                for file in os.listdir(path):
                    if file.endswith(".xml") or file.endswith(".xmltei"):
                        file_path = os.path.join(path, file)
                        xml_files.append(file_path)
    
    if not xml_files:
        st.error("No XML files found. Please upload XML files or use the default corpus.")
        return documents, document_dates
    
    # Create a progress bar
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Process files with progress updates
    for i, file_path in enumerate(xml_files):
        progress = (i) / len(xml_files)
        progress_bar.progress(progress)
        status_text.text(f"Traitement du fichier {i+1}/{len(xml_files)}: {os.path.basename(file_path)}")
        
        doc_data = parse_xmltei_document(file_path)
        
        if doc_data:
            doc = Document(
                page_content=doc_data["text"],
                metadata={
                    "source": file_path,
                    "title": doc_data["title"],
                    "date": doc_data["date"],
                    "year": doc_data["year"],
                    "persons": doc_data["persons"]
                }
            )
            documents.append(doc)
            
            if doc_data["year"]:
                document_dates[file_path] = doc_data["year"]
    
    progress_bar.progress(1.0)
    status_text.text(f"Traitement terminé! {len(documents)} documents analysés.")
    
    return documents, document_dates

def split_documents(documents):
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=2500, chunk_overlap=800)
    texts = text_splitter.split_documents(documents)
    return texts

def load_precomputed_embeddings():
    """Load precomputed embeddings from the embeddings directory."""
    embeddings_path = EMBEDDINGS_DIR / "faiss_index"
    metadata_path = EMBEDDINGS_DIR / "document_metadata.pkl"
    
    if not embeddings_path.exists():
        st.error(f"Pre-computed embeddings folder not found at {embeddings_path}")
        return None
        
    if not (embeddings_path / "index.faiss").exists():
        st.error(f"FAISS index file not found at {embeddings_path}/index.faiss")
        return None
        
    if not (embeddings_path / "index.pkl").exists():
        st.error(f"Index pickle file not found at {embeddings_path}/index.pkl")
        return None
    
    embedding_model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    
    if metadata_path.exists():
        try:
            with open(metadata_path, "rb") as f:
                metadata = pickle.load(f)
                st.success(f"Loaded pre-computed embeddings with {metadata['chunk_count']} chunks from {metadata['document_count']} documents")
                
                if 'model_name' in metadata:
                    embedding_model = metadata['model_name']
                    st.info(f"Embedding model: {embedding_model}")
                else:
                    st.warning("Model information not found in metadata, using default model")
        except Exception as e:
            st.warning(f"Error loading metadata: {str(e)}")
            st.warning("Using default embedding model")
    else:
        st.warning("Metadata file not found. Using default embedding model.")
    
    try:
        embeddings = HuggingFaceEmbeddings(
            model_name=embedding_model,
            model_kwargs={"device": "cpu"}
        )
        
        try:
            st.info(f"Loading FAISS index with model: {embedding_model}")
            vectordb = FAISS.load_local(
                embeddings_path.as_posix(), 
                embeddings,
                allow_dangerous_deserialization=True
            )
            
            retriever = vectordb.as_retriever(
                search_type="mmr", 
                search_kwargs={'k': 3, 'fetch_k': 20}
            )
            
            st.success("FAISS index loaded successfully!")
            return retriever
            
        except Exception as e:
            st.error(f"Error loading FAISS index: {str(e)}")
            st.error("Unable to load pre-computed embeddings. You'll need to process documents instead.")
            return None
    
    except Exception as e:
        st.error(f"Error in embeddings initialization: {str(e)}")
        return None

def embeddings_on_local_vectordb(texts, hf_api_key):
    """Create embeddings and store in a local vector database using FAISS."""
    import os
    os.environ["HUGGINGFACE_HUB_TOKEN"] = hf_api_key
    
    model_kwargs = {"token": hf_api_key}
    model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    
    embeddings = HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={**model_kwargs, "device": "cpu"}
    )
    
    try:
        vectordb = FAISS.from_documents(texts, embeddings)
        
        LOCAL_VECTOR_STORE_DIR.mkdir(parents=True, exist_ok=True)
        vectordb.save_local(LOCAL_VECTOR_STORE_DIR.as_posix())
        
        with open(LOCAL_VECTOR_STORE_DIR / "model_info.pkl", "wb") as f:
            pickle.dump({
                "model_name": model_name,
                "chunk_count": len(texts)
            }, f)
        
        retriever = vectordb.as_retriever(
            search_type="mmr", 
            search_kwargs={'k': 3, 'fetch_k': 20}
        )
        
        return retriever
        
    except Exception as e:
        st.error(f"Error creating embeddings: {str(e)}")
        
        try:
            st.info("Trying batch processing approach...")
            
            batch_size = 50
            batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
            
            vectordb = FAISS.from_documents(batches[0], embeddings)
            
            for i, batch in enumerate(batches[1:], 1):
                st.info(f"Processing batch {i+1}/{len(batches)}...")
                vectordb.add_documents(batch)
            
            vectordb.save_local(LOCAL_VECTOR_STORE_DIR.as_posix())
            
            with open(LOCAL_VECTOR_STORE_DIR / "model_info.pkl", "wb") as f:
                pickle.dump({
                    "model_name": model_name,
                    "chunk_count": len(texts)
                }, f)
            
            retriever = vectordb.as_retriever(
                search_type="mmr", 
                search_kwargs={'k': 3, 'fetch_k': 20}
            )
            
            return retriever
            
        except Exception as batch_e:
            st.error(f"Error with batch processing: {str(batch_e)}")
            return None


def query_llm(retriever, query, hf_api_key, openai_api_key=None, openrouter_api_key=None, model_choice="openrouter"):
    """Query the LLM using one of the supported models with improved error handling."""
    import streamlit as st
    from langchain_community.llms import HuggingFaceHub

    progress_container = st.empty()
    progress_container.info("Recherche des documents pertinents...")
    progress_bar = st.progress(0)

    try:
        # Use invoke instead of get_relevant_documents
        relevant_docs = retriever.invoke(query)

        # --- DEBUG START ---
        print(f"\n--- DEBUG: Retrieved {len(relevant_docs)} relevant documents ---")
        retrieved_content_length = 0
        for i, doc in enumerate(relevant_docs):
            print(f"Source {i+1} Title: {doc.metadata.get('title', 'N/A')}")
            print(f"Source {i+1} Full Content:")
            print(doc.page_content)
            print("-" * 50)
            retrieved_content_length += len(doc.page_content)
        print(f"Total retrieved content length: {retrieved_content_length} characters")
        print("--------------------------------------------------")
        # --- DEBUG END ---

        if not relevant_docs:
            st.warning("Aucun document pertinent trouvé pour cette requête.")
            return "Aucun document pertinent n'a été trouvé pour répondre à votre question.", []

        # Create context from relevant documents
        context_parts = []
        source_mapping = []
        for i, doc in enumerate(relevant_docs):
            doc_title = doc.metadata.get('title', 'Document sans titre')
            doc_date = doc.metadata.get('date', 'Date inconnue')
            source_mapping.append(f"Source {i+1}: {doc_title} | {doc_date}")
            context_parts.append(f"Source {i+1}:\nTitle: {doc_title}\nDate: {doc_date}\nContent: {doc.page_content}\n")

        context = "\n".join(context_parts)
        source_references = "\n".join(source_mapping)

        # Format the query using the template from session state
        base_query_template = st.session_state.query_prompt
        formatted_query = base_query_template.format(query=query)

        # Create the complete prompt with system instructions
        system_prompt = """Tu es un agent RAG chargé de générer des réponses en t'appuyant exclusivement sur les informations fournies dans les documents de référence.

IMPORTANT: Pour chaque information ou affirmation dans ta réponse, tu DOIS indiquer explicitement le numéro de la source (Source 1, Source 2, etc.) dont provient cette information."""

        # Additional instructions for source referencing
        additional_instructions = f"""

INSTRUCTIONS IMPORTANTES:
- Pour CHAQUE fait ou information mentionné dans ta réponse, indique EXPLICITEMENT le numéro de la source correspondante (ex: Source 1, Source 3)
- Cite les sources même pour les informations de confiance élevée
- Fais référence aux sources numérotées ci-dessous dans chaque section de ta réponse

SOURCES DISPONIBLES:
{source_references}

CONTEXTE DOCUMENTAIRE:
{context}
"""

        # Complete user message
        user_message = f"{formatted_query}{additional_instructions}"

        # --- DEBUG START ---
        print(f"\n--- DEBUG: System Prompt Length: {len(system_prompt)} chars ---")
        print(f"--- DEBUG: User Message Length: {len(user_message)} chars ---")
        print(f"--- DEBUG: User Message (first 1000 chars) ---")
        print(user_message[:1000])
        print("...")
        # --- DEBUG END ---

        progress_bar.progress(0.3)
        progress_container.info("Initialisation du modèle...")

        # Initialize client and get response based on model choice
        answer = None
        
        try:
            if model_choice == "openrouter" or model_choice == "llama":
                if not openrouter_api_key:
                    st.error("OpenRouter API key is required to use OpenRouter models")
                    return None, None

                progress_container.info("Utilisation d'OpenRouter avec Llama 4 Maverick...")
                
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import HumanMessage, SystemMessage
                
                llm = ChatOpenAI(
                    temperature=0.7,
                    model_name="meta-llama/llama-4-maverick:free",
                    openai_api_key=openrouter_api_key,
                    max_tokens=2000,
                    openai_api_base="https://openrouter.ai/api/v1",
                    default_headers={
                        "HTTP-Referer": "https://streamlit-rag-app.com",
                        "X-Title": "Streamlit RAG App"
                    }
                )
                
                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_message)
                ]
                
                response = llm.invoke(messages)
                # Proper extraction from LangChain response
                answer = response.content if hasattr(response, 'content') else str(response)

            elif model_choice == "gemma":
                if not openrouter_api_key:
                    st.error("OpenRouter API key is required to use Gemma model")
                    return None, None

                progress_container.info("Utilisation d'OpenRouter avec Gemma...")
                
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import HumanMessage
                
                llm = ChatOpenAI(
                    temperature=0.7,
                    model_name="google/gemma-3n-e4b-it:free",
                    openai_api_key=openrouter_api_key,
                    max_tokens=2000,
                    openai_api_base="https://openrouter.ai/api/v1",
                    default_headers={
                        "HTTP-Referer": "https://streamlit-rag-app.com",
                        "X-Title": "Streamlit RAG App"
                    }
                )
                
                # Gemma doesn't support system messages, combine into single user message
                combined_message = f"{system_prompt}\n\n{user_message}"
                messages = [HumanMessage(content=combined_message)]
                
                response = llm.invoke(messages)
                # Proper extraction from LangChain response
                answer = response.content if hasattr(response, 'content') else str(response)

            elif model_choice == "qwen":
                if not openrouter_api_key:
                    st.error("OpenRouter API key is required to use Qwen model")
                    return None, None

                progress_container.info("Utilisation d'OpenRouter avec Qwen3 32B...")
                
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import HumanMessage, SystemMessage
                
                llm = ChatOpenAI(
                    temperature=0.7,
                    model_name="qwen/qwen3-32b:free",
                    openai_api_key=openrouter_api_key,
                    max_tokens=2000,
                    openai_api_base="https://openrouter.ai/api/v1",
                    default_headers={
                        "HTTP-Referer": "https://streamlit-rag-app.com",
                        "X-Title": "Streamlit RAG App"
                    }
                )
                
                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_message)
                ]
                
                response = llm.invoke(messages)
                # Proper extraction from LangChain response
                answer = response.content if hasattr(response, 'content') else str(response)

            elif model_choice == "mistral":
                if not hf_api_key:
                    st.error("Hugging Face API key is required to use Mistral model")
                    return None, None

                progress_container.info("Utilisation de Hugging Face avec Mistral...")
                
                # For HuggingFace models, keep the existing approach
                llm = HuggingFaceHub(
                    repo_id="mistralai/Mistral-7B-Instruct-v0.3",
                    huggingfacehub_api_token=hf_api_key,
                    model_kwargs={
                        "temperature": 0.7,
                        "max_new_tokens": 1000,
                        "top_p": 0.95,
                        "do_sample": True,
                        "return_full_text": False
                    }
                )
                
                # Combine system and user message for HuggingFace
                complete_prompt = f"{system_prompt}\n\n{user_message}"
                response = llm.invoke(complete_prompt)
                answer = response if isinstance(response, str) else str(response)

            elif model_choice == "zephyr":
                if not hf_api_key:
                    st.error("Hugging Face API key is required to use Zephyr model")
                    return None, None

                progress_container.info("Utilisation de Hugging Face avec Zephyr...")
                
                llm = HuggingFaceHub(
                    repo_id="HuggingFaceH4/zephyr-7b-beta",
                    huggingfacehub_api_token=hf_api_key,
                    model_kwargs={
                        "temperature": 0.7,
                        "max_new_tokens": 1000,
                        "top_p": 0.95,
                        "do_sample": True,
                        "return_full_text": False
                    }
                )
                
                # Combine system and user message for HuggingFace
                complete_prompt = f"{system_prompt}\n\n{user_message}"
                response = llm.invoke(complete_prompt)
                answer = response if isinstance(response, str) else str(response)

            else:
                # Default fallback to Llama if unknown model choice
                if not openrouter_api_key:
                    st.error("OpenRouter API key is required for the default Llama model")
                    return None, None

                progress_container.info("Utilisation d'OpenRouter avec Llama 4 Maverick (par défaut)...")
                
                from langchain_openai import ChatOpenAI
                from langchain_core.messages import HumanMessage, SystemMessage
                
                llm = ChatOpenAI(
                    temperature=0.7,
                    model_name="meta-llama/llama-4-maverick:free",
                    openai_api_key=openrouter_api_key,
                    max_tokens=2000,
                    openai_api_base="https://openrouter.ai/api/v1",
                    default_headers={
                        "HTTP-Referer": "https://streamlit-rag-app.com",
                        "X-Title": "Streamlit RAG App"
                    }
                )
                
                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_message)
                ]
                
                response = llm.invoke(messages)
                # Proper extraction from LangChain response
                answer = response.content if hasattr(response, 'content') else str(response)

        except Exception as e:
            st.error(f"Error during LLM invocation: {str(e)}")
            print(f"LLM invocation error: {str(e)}")
            print(f"Error type: {type(e)}")
            return None, None

        # Check if we got a valid answer
        if answer is None or answer.strip() == "":
            st.error("Failed to get response from LLM")
            return None, None

        # --- DEBUG START ---
        print(f"\n--- DEBUG: Final Answer ---")
        print(f"Answer type: {type(answer)}")
        print(f"Answer length: {len(answer)} chars")
        print(f"Answer content (first 500 chars): {answer[:500]}")
        print("----------------------------")
        # --- DEBUG END ---

        progress_bar.progress(0.9)
        progress_container.info("Finalisation et mise en forme de la réponse...")

        # Clean up the answer
        answer = answer.strip()

        # Update message history
        if "messages" in st.session_state:
            st.session_state.messages.append((query, answer))

        progress_bar.progress(1.0)
        progress_container.empty()

        return answer, relevant_docs

    except Exception as e:
        progress_container.error(f"Erreur pendant la génération: {str(e)}")
        print(f"General error in query_llm: {str(e)}")
        print(f"Error type: {type(e)}")
        st.exception(e)
        return None, None

def process_documents(hf_api_key, use_uploaded_only):
    if not hf_api_key:
        st.warning("Please provide the Hugging Face API key.")
        return None
    
    try:
        status_container = st.empty()
        status_container.info("Chargement des documents...")
        
        documents, document_dates = load_documents(use_uploaded_only)
        if not documents:
            st.error("No documents found to process.")
            return None
        
        status_container.info("Découpage des documents en fragments...")
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=2500, chunk_overlap=800)
        texts = text_splitter.split_documents(documents)
        
        status_container.info("Création des embeddings (cela peut prendre plusieurs minutes)...")
        progress_bar = st.progress(0)
        
        progress_bar.progress(0.2)
        
        retriever = embeddings_on_local_vectordb(texts, hf_api_key)
        
        progress_bar.progress(0.8)
        status_container.info("Finalisation...")
        
        progress_bar.progress(1.0)
        status_container.success(f"Traitement terminé! {len(texts)} fragments créés à partir de {len(documents)} documents.")
        
        return retriever
        
    except Exception as e:
        st.error(f"Une erreur s'est produite: {e}")
        st.exception(e)  # Show full traceback for debugging
        return None

def input_fields():
    """Set up the input fields in the sidebar with improved responsive layout."""
    with st.sidebar:
        st.markdown("""
        <style>
        .stSelectbox, .stRadio > div, .stExpander, [data-testid="stFileUploader"] {
            max-width: 100%;
            overflow-x: hidden;
        }
        .stCheckbox label p {
            font-size: 14px;
            margin-bottom: 0;
            white-space: normal;
        }
        div.row-widget.stRadio > div {
            flex-direction: column;
            margin-top: -10px;
        }
        div.row-widget.stRadio > div label {
            margin: 0;
            padding: 2px 0;
        }
        .stExpander {
            font-size: 14px;
        }
        .stExpander details summary p {
            margin-bottom: 0;
        }
        .stExpander details summary::marker {
            margin-right: 5px;
        }
        </style>
        """, unsafe_allow_html=True)
        
        st.title("Configuration")
        
        # Hugging Face API Key
        if "hf_api_key" in st.secrets:
            st.session_state.hf_api_key = st.secrets.hf_api_key
        else:
            st.session_state.hf_api_key = st.text_input("Hugging Face API Key", type="password", key="hf_key")
        
        # OpenRouter API Key
        if "openrouter_api_key" in st.secrets:
            st.session_state.openrouter_api_key = st.secrets.openrouter_api_key
        else:
            st.session_state.openrouter_api_key = st.text_input("OpenRouter API Key (Llama 4)", type="password", key="openrouter_key")
            
        # Add option to use pre-computed embeddings
        embeddings_path = EMBEDDINGS_DIR / "faiss_index"
        embeddings_available = embeddings_path.exists()
        
        st.session_state.use_precomputed = st.checkbox(
            "Utiliser embeddings pré-calculés",
            value=embeddings_available,
            disabled=not embeddings_available,
            key="use_precomputed_cb"
        )
        
        if embeddings_available and st.session_state.use_precomputed:
            metadata_path = EMBEDDINGS_DIR / "document_metadata.pkl"
            if metadata_path.exists():
                try:
                    with open(metadata_path, "rb") as f:
                        metadata = pickle.load(f)
                        st.info(f"Modèle: {metadata.get('model_name', 'Unknown')}")
                except:
                    pass
            
            st.markdown("---")
            
        # Model selection
        st.session_state.model_choice = st.radio(
            "Modèle LLM",
            ["llama", "zephyr", "mistral", "gemma","qwen"], 
            format_func=lambda x: {
                "llama": "Llama",
                "zephyr": "Zephyr",
                "mistral": "Mistral",
                "gemma": "Gemma",
                "qwen":" Qwen"
            }[x],
            horizontal=False,
            key="model_choice_radio"
        )

        # Model information
        with st.expander("Infos modèle", expanded=False):
            if st.session_state.model_choice == "zephyr":
                st.markdown("""
                **Zephyr-7b-beta**
                
                * Bonne compréhension des instructions
                * Précision factuelle solide
                """)
            elif st.session_state.model_choice == "mistral":
                st.markdown("""
                **Mistral-7B-Instruct-v0.3**
                
                * Raisonnement sur documents scientifiques
                * Bonne extraction d'informations
                * Réponses structurées en français
                """)
            elif st.session_state.model_choice == "gemma":
                st.markdown("""
                **Gemma-3n-e4b-it**
                
                * Fenêtre contextuelle 32K tokens
                * Multilingue (140+ langues)
                """)
            elif st.session_state.model_choice == "llama":
                st.markdown("""
                **Llama 4 Maverick**
                
                * Dernière génération de Llama
                * Performances supérieures
                * Excellente compréhension du français
                """)
            elif st.session_state.model_choice == "qwen":
                st.markdown("""
                **Qwen3-32B**
                
                * Excellente logique et raisonnement  
                * Contexte étendu jusqu’à 131K tokens  
                * Très bon en RAG multilingue
                """)
        
        # Prompt configuration
        with st.expander("Configuration du prompt (COSTAR)", expanded=False):
            if "query_prompt" not in st.session_state:
                st.session_state.query_prompt = DEFAULT_QUERY_PROMPT
            
            st.markdown("##### Framework COSTAR")
            st.markdown("*Méthodologie structurée pour des réponses précises*")
            
            st.info("""
            **COSTAR** est un framework de prompting structuré:
            - **C**ontexte: Le cadre de l'analyse
            - **O**bjectif: But précis de la requête
            - **S**tyle: Format et structure
            - **T**on: Registre de langage
            - **A**udience: Destinataires de la réponse
            - **R**éponse: Format attendu
            """)
            
            st.markdown("##### Prompt de requête")
            st.session_state.query_prompt = st.text_area(
                "Query prompt",
                value=st.session_state.query_prompt,
                height=300,
                key="query_prompt_area"
            )
            
            if st.button("Réinitialiser le prompt", key="reset_prompt_btn"):
                st.session_state.query_prompt = DEFAULT_QUERY_PROMPT
                st.rerun()
            
        # Initialize uploaded_files in session state if not present
        if "uploaded_files" not in st.session_state:
            st.session_state.uploaded_files = []

        st.markdown("### Fichiers XML")
        
        # File uploader
        uploaded_files = st.file_uploader("Télécharger", 
                                         type=["xml", "xmltei"], 
                                         accept_multiple_files=True,
                                         key="file_uploader")
        
        # Process uploaded files and store them in session state
        if uploaded_files:
            new_files = []
            os.makedirs("data/uploaded", exist_ok=True)
            
            for uploaded_file in uploaded_files:
                file_path = os.path.join("data/uploaded", uploaded_file.name)
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                new_files.append(file_path)
            
            for file_path in new_files:
                if file_path not in st.session_state.uploaded_files:
                    st.session_state.uploaded_files.append(file_path)
            
            if len(new_files) > 0:
                st.success(f"{len(new_files)} fichier(s) sauvegardé(s)")
        
        # Display checkbox for using only uploaded files
        st.session_state.use_uploaded_only = st.checkbox(
            "Utiliser uniquement fichiers téléchargés",
            value=bool(st.session_state.uploaded_files),
            key="use_uploaded_only_cb"
        )
        
        if st.session_state.use_uploaded_only and not st.session_state.uploaded_files:
            st.warning("Aucun fichier téléchargé")
        
        # Display the list of uploaded files
        if st.session_state.uploaded_files:
            total_files = len(st.session_state.uploaded_files)
            with st.expander(f"Fichiers ({total_files})", expanded=False):
                file_list_html = "<div style='max-height: 150px; overflow-y: auto;'>"
                for file_path in st.session_state.uploaded_files:
                    file_name = os.path.basename(file_path)
                    file_list_html += f"<div style='padding: 2px 0; font-size: 13px;'>✓ {file_name}</div>"
                file_list_html += "</div>"
                st.markdown(file_list_html, unsafe_allow_html=True)
                
                if st.button("Effacer tous", key="clear_files"):
                    st.session_state.uploaded_files = []
                    st.rerun()

def boot():
    """Main function to run the application."""
    # Initialize query prompt if not present
    if "query_prompt" not in st.session_state:
        st.session_state.query_prompt = DEFAULT_QUERY_PROMPT
    
    # Setup input fields
    input_fields()
    
    # Initialize session state
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    if "retriever" not in st.session_state:
        st.session_state.retriever = None
    
    # Add buttons for different processing methods
    col1, col2 = st.columns(2)

    # Button for pre-computed embeddings
    if st.session_state.use_precomputed:
        with col1:
            if st.button("Charger embeddings pré-calculés", use_container_width=True):
                with st.spinner("Chargement des embeddings pré-calculés..."):
                    st.session_state.retriever = load_precomputed_embeddings()
    
    # Button for processing documents - Always show when there are uploaded files
    if not st.session_state.use_precomputed or st.session_state.uploaded_files:
        with col1:  # Keep it in the left column
            if st.button("Traiter les documents", use_container_width=True):
                st.session_state.retriever = process_documents(
                    st.session_state.hf_api_key,  
                    st.session_state.use_uploaded_only
                )

    
    # Display chat history
    for message in st.session_state.messages:
        st.chat_message('human').write(message[0])
        st.chat_message('ai').write(message[1])
    
    # Chat input
    if query := st.chat_input("Posez votre question..."):
        if not st.session_state.retriever:
            st.error("Veuillez d'abord charger les embeddings ou traiter les documents.")
            return
        
        st.chat_message("human").write(query)
        
        with st.spinner("Génération de la réponse..."):
            try:
                # Check model requirements - GPT check commented out
                # if st.session_state.model_choice == "gpt" and not st.session_state.openai_api_key:
                #     st.error("La clé API OpenAI est requise pour utiliser le modèle GPT-3.5.")
                #     return
                
                # For backward compatibility, still pass openai_api_key even though it's not used
                answer, source_docs = query_llm(
                    st.session_state.retriever,  
                    query,  
                    st.session_state.hf_api_key,
                    None,  # openai_api_key set to None
                    st.session_state.openrouter_api_key,  
                    st.session_state.model_choice
                )
                
                # Display the answer with markdown support
                response_container = st.chat_message("ai")
                response_container.markdown(answer)
                
                if source_docs:
                    response_container.markdown("---")
                    response_container.markdown("**Sources:**")
                    
                    # Create an expander for each source
                    for i, doc in enumerate(source_docs):
                        # Prepare document info
                        doc_title = doc.metadata.get('title', 'Document sans titre')
                        doc_date = doc.metadata.get('date', 'Date inconnue')
                        doc_year = doc.metadata.get('year', '')
                        doc_file = doc.metadata.get('source', 'Fichier inconnu')
                        
                        # Use expander as a button-like interface
                        with response_container.expander(f"📄 Source {i+1}: {doc_title}", expanded=False):
                            st.markdown(f"**Date:** {doc_date}")
                            st.markdown(f"**Fichier:** {doc_file}")
                            
                            # Show persons if available
                            if doc.metadata.get('persons'):
                                persons = doc.metadata.get('persons')
                                if isinstance(persons, list) and persons:
                                    st.markdown("**Personnes mentionnées:**")
                                    st.markdown(", ".join(persons))
                            
                            # Show content
                            st.markdown("**Extrait:**")
                            content = doc.page_content
                            # Clean up content if needed
                            if content.startswith(f"Document: {doc_title}"):
                                content = content.replace(f"Document: {doc_title} | Date: {doc_date}\n\n", "")
                            
                            st.text_area("", value=content, height=150, disabled=True)
                
            except Exception as e:
                st.error(f"Error generating response: {e}")

if __name__ == '__main__':
    boot()
