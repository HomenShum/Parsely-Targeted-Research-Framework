"""
File processing module for handling document ingestion, chunking, and metadata generation.
"""

import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from pydantic import BaseModel, Field
from dataclasses import dataclass
from llama_index.core.schema import Document, TextNode
from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, AsyncQdrantClient
from pydantic_ai import Agent
import pandas as pd
from .method_recommendation import ProcessingMethod

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

@dataclass
class ProcessingConfig:
    """Configuration for file processing."""
    azure_openai_key: Optional[str] = None
    openai_key: Optional[str] = None
    azure_endpoint: Optional[str] = None
    embedding_model: str = "text-embedding-3-small"
    processing_method: ProcessingMethod = ProcessingMethod.PARSE_API_URL
    session_id: Optional[str] = None

class FileSummary(BaseModel):
    """Overall summary of a processed file"""
    file_name: str
    total_chunks: int
    processing_method: ProcessingMethod
    session_id: str
    creation_time: datetime = Field(default_factory=datetime.utcnow)
    summary: str
    key_points: List[str]
    document_type: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

class AgentMetadata(BaseModel):
    """Metadata generated by the agent."""
    title: str
    hashtags: List[str] = Field(description="List of hashtags for the document")
    hypothetical_questions: List[str]
    summary: str

class DocumentInfo(BaseModel):
    """Complete document information combining external and agent-generated data."""
    source_name: str
    index: int
    text_chunk: str
    title: str
    hashtags: List[str]
    hypothetical_questions: List[str]
    summary: str
    metadata: Dict[str, Any]

class ProcessingResult(BaseModel):
    """Processing result with error handling"""
    success: bool
    message: str
    method_used: ProcessingMethod
    document_info: Optional[List[DocumentInfo]] = None
    error: Optional[str] = None

class DocumentSummaryMetadata(BaseModel):
    """Overall document summary metadata"""
    summary: str
    key_points: List[str] = Field(description="Key points from all chunks")
    document_type: str
    themes: List[str] = Field(description="Main themes across all chunks")
    all_hashtags: List[str] = Field(description="Combined unique hashtags")
    key_questions: List[str] = Field(description="Selected important questions")

# Initialize agents for metadata generation
generate_document_metadata_async_agent = Agent(
    model="openai:gpt-4o-mini",
    result_type=AgentMetadata,
    system_prompt="""You are an expert at analyzing document content and generating metadata.
    Extract key information including title, hashtags, and potential questions.
    Focus on the main themes and concepts present in the text."""
)

document_summary_agent = Agent(
    model="openai:gpt-4o",
    result_type=DocumentSummaryMetadata,
    system_prompt="""You are an expert at summarizing document content and identifying key themes.
    Analyze the provided chunks to create a comprehensive summary and extract key points.
    Focus on maintaining context across all chunks while highlighting important details."""
)

# File type categories
FILE_CATEGORIES = {
    "pdf": ["pdf", "docx", "doc", "odt", "pptx", "ppt"],
    "image": ["png", "jpg", "jpeg"],
    "excel": ["xlsx", "xls"],
    "csv": ["csv"],
    "other": ["txt", "json", "xml"]
}

def retry_async(retries=3, delay=1):
    """Retry decorator for async functions"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
            raise last_exception
        return wrapper
    return decorator

class RateLimitError(Exception):
    """Exception raised when API rate limits are hit."""
    pass

class ProcessingError(Exception):
    """Exception raised when document processing fails."""
    pass

@retry_async(retries=3, delay=2)
async def handle_rate_limited_request(func, *args, **kwargs):
    """
    Handle rate-limited API requests with retries.
    
    Args:
        func: Async function to call
        *args: Positional arguments for func
        **kwargs: Keyword arguments for func
        
    Returns:
        Result from the function call
        
    Raises:
        RateLimitError: If rate limit is hit after all retries
        ProcessingError: If processing fails for other reasons
    """
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        if "rate limit" in str(e).lower():
            raise RateLimitError(f"Rate limit exceeded: {str(e)}")
        raise ProcessingError(f"Processing failed: {str(e)}")

async def file_processing_pipeline_step4_process_document(
    file_data: bytes,
    filename: str,
    config: ProcessingConfig,
    sem: asyncio.Semaphore,
    update_file_status_func: Optional[callable] = None,
    progress_bar: Optional[Any] = None
) -> ProcessingResult:
    """Process a single document file."""
    try:
        # Implementation details here
        pass
    except Exception as e:
        return ProcessingResult(
            success=False,
            message=f"Error processing document: {str(e)}",
            method_used=config.processing_method,
            error=str(e)
        )

@retry_async(retries=3, delay=1)
async def file_processing_pipeline_step5_generate_document_metadata_async(
    message_data: str,
    sem: asyncio.Semaphore,
    filename: str,
    index: int
) -> Dict[str, Any]:
    """Generate metadata for a document chunk using rate-limited API calls."""
    async with sem:
        try:
            result = await generate_document_metadata_async_agent.run(
                RunContext(inputs={"content": message_data})
            )
            return result.dict()
        except Exception as e:
            logger.error(f"Error generating metadata for {filename} chunk {index}: {str(e)}")
            return {}

async def file_processing_pipeline_generate_document_metadata_overall_summary(
    chunks: List[str],
    filename: str,
    config: ProcessingConfig
) -> DocumentSummaryMetadata:
    """Generate overall document summary from processed chunks."""
    try:
        result = await document_summary_agent.run(
            RunContext(inputs={"chunks": chunks, "filename": filename})
        )
        return result
    except Exception as e:
        logger.error(f"Error generating overall summary for {filename}: {str(e)}")
        return DocumentSummaryMetadata(
            summary="Error generating summary",
            key_points=[],
            document_type="unknown",
            themes=[],
            all_hashtags=[],
            key_questions=[]
        )

def file_processing_pipeline_step1_categorize_files(uploaded_files: List) -> Dict[str, List]:
    """Categorize uploaded files based on their extensions."""
    categorized_files = {category: [] for category in FILE_CATEGORIES.keys()}
    
    for file in uploaded_files:
        file_ext = Path(file.name).suffix.lower().lstrip('.')
        for category, extensions in FILE_CATEGORIES.items():
            if file_ext in extensions:
                categorized_files[category].append(file)
                break
    
    return categorized_files

async def process_files(
    uploaded_files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None,
    progress_bar: Optional[Any] = None
) -> Dict[str, FileSummary]:
    """Main entry point for processing uploaded files."""
    categorized_files = file_processing_pipeline_step1_categorize_files(uploaded_files)
    processed_files_summary = {}
    
    # Process each category of files
    for category, files in categorized_files.items():
        if not files:
            continue
            
        if category == "pdf":
            results = await process_pdf_files(files, config, update_file_status_func, progress_bar)
        elif category == "image":
            results = await process_image_files(files, config, update_file_status_func, progress_bar)
        elif category == "excel":
            results = await process_excel_files(files, config, update_file_status_func)
        elif category == "csv":
            results = await process_csv_files(files, config, update_file_status_func)
        else:
            results = await process_other_files(files, config, update_file_status_func, progress_bar)
            
        processed_files_summary.update(results)
    
    return processed_files_summary

async def process_pdf_files(
    files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None,
    progress_bar: Optional[Any] = None
) -> Dict[str, FileSummary]:
    """Process PDF files concurrently."""
    processed_files = {}
    
    async def process_single_pdf(file):
        try:
            result = await file_processing_pipeline_step4_process_document(
                file_data=file.getvalue(),
                filename=file.name,
                config=config,
                sem=asyncio.Semaphore(5),  # Limit concurrent processing
                update_file_status_func=update_file_status_func,
                progress_bar=progress_bar
            )
            
            if result.success and result.document_info:
                # Process chunks concurrently
                await asyncio.gather(*[
                    file_processing_pipeline_step6_add_unified_document_chunk(
                        source_name=doc_info.source_name,
                        index=doc_info.index,
                        file_type="pdf",
                        text_chunk=doc_info.text_chunk,
                        title=doc_info.title,
                        hashtags=doc_info.hashtags,
                        hypothetical_questions=doc_info.hypothetical_questions,
                        summary=doc_info.summary,
                        metadata=doc_info.metadata,
                        config=config
                    )
                    for doc_info in result.document_info
                ])
                
                # Generate overall summary
                summary = await file_processing_pipeline_generate_document_metadata_overall_summary(
                    chunks=[doc.text_chunk for doc in result.document_info],
                    filename=file.name,
                    config=config
                )
                
                processed_files[file.name] = FileSummary(
                    file_name=file.name,
                    total_chunks=len(result.document_info),
                    processing_method=config.processing_method,
                    session_id=config.session_id,
                    summary=summary.summary,
                    key_points=summary.key_points,
                    document_type=summary.document_type,
                    metadata={
                        "themes": summary.themes,
                        "all_hashtags": summary.all_hashtags,
                        "key_questions": summary.key_questions
                    }
                )
                
                if update_file_status_func:
                    update_file_status_func(file.name, "✅ Processing complete")
                    
            else:
                if update_file_status_func:
                    update_file_status_func(file.name, f"❌ Error: {result.error}")
                logger.error(f"Error processing PDF {file.name}: {result.error}")
                
        except Exception as e:
            logger.error(f"Error processing PDF {file.name}: {str(e)}")
            if update_file_status_func:
                update_file_status_func(file.name, f"❌ Error: {str(e)}")
    
    # Process all PDFs concurrently
    await asyncio.gather(*[process_single_pdf(file) for file in files])
    return processed_files

async def process_image_files(
    files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None,
    progress_bar: Optional[Any] = None
) -> Dict[str, FileSummary]:
    """Process image files concurrently."""
    processed_files = {}
    
    async def process_single_image(file):
        try:
            result = await file_processing_pipeline_step4_process_document(
                file_data=file.getvalue(),
                filename=file.name,
                config=config,
                sem=asyncio.Semaphore(3),  # Lower concurrency for images
                update_file_status_func=update_file_status_func,
                progress_bar=progress_bar
            )
            
            if result.success and result.document_info:
                # Process chunks concurrently
                await asyncio.gather(*[
                    file_processing_pipeline_step6_add_unified_document_chunk(
                        source_name=doc_info.source_name,
                        index=doc_info.index,
                        file_type="image",
                        text_chunk=doc_info.text_chunk,
                        title=doc_info.title,
                        hashtags=doc_info.hashtags,
                        hypothetical_questions=doc_info.hypothetical_questions,
                        summary=doc_info.summary,
                        metadata=doc_info.metadata,
                        config=config
                    )
                    for doc_info in result.document_info
                ])
                
                # Generate overall summary
                summary = await file_processing_pipeline_generate_document_metadata_overall_summary(
                    chunks=[doc.text_chunk for doc in result.document_info],
                    filename=file.name,
                    config=config
                )
                
                processed_files[file.name] = FileSummary(
                    file_name=file.name,
                    total_chunks=len(result.document_info),
                    processing_method=config.processing_method,
                    session_id=config.session_id,
                    summary=summary.summary,
                    key_points=summary.key_points,
                    document_type=summary.document_type,
                    metadata={
                        "themes": summary.themes,
                        "all_hashtags": summary.all_hashtags,
                        "key_questions": summary.key_questions
                    }
                )
                
                if update_file_status_func:
                    update_file_status_func(file.name, "✅ Processing complete")
                    
            else:
                if update_file_status_func:
                    update_file_status_func(file.name, f"❌ Error: {result.error}")
                logger.error(f"Error processing image {file.name}: {result.error}")
                
        except Exception as e:
            logger.error(f"Error processing image {file.name}: {str(e)}")
            if update_file_status_func:
                update_file_status_func(file.name, f"❌ Error: {str(e)}")
    
    # Process all images concurrently
    await asyncio.gather(*[process_single_image(file) for file in files])
    return processed_files

async def process_excel_files(
    files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None
) -> Dict[str, FileSummary]:
    """Process Excel files concurrently."""
    processed_files = {}
    
    async def process_single_excel(file):
        try:
            if update_file_status_func:
                update_file_status_func(file.name, "🔍 Processing Excel file...")
            
            # Use ThreadPoolExecutor for pandas operations
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                df = await loop.run_in_executor(pool, pd.read_excel, file)
            
            chunks = []
            for idx, row in df.iterrows():
                text_chunk = f"Row {idx}: " + ", ".join([f"{col}: {val}" for col, val in row.items()])
                chunks.append({
                    "text": text_chunk,
                    "metadata": {
                        "row": idx,
                        "original_values": row.to_dict()
                    }
                })
            
            # Process chunks concurrently
            chunk_results = await asyncio.gather(*[
                file_processing_pipeline_step5_generate_metadata(
                    chunk["text"],
                    file.name,
                    idx,
                    config
                )
                for idx, chunk in enumerate(chunks)
            ])
            
            # Add chunks to unified collection
            await asyncio.gather(*[
                file_processing_pipeline_step6_add_unified_document_chunk(
                    source_name=file.name,
                    index=idx,
                    file_type="excel",
                    text_chunk=chunk["text"],
                    title=result.get("title", ""),
                    hashtags=result.get("hashtags", []),
                    hypothetical_questions=result.get("hypothetical_questions", []),
                    summary=result.get("summary", ""),
                    metadata={**chunk["metadata"], **result.get("metadata", {})},
                    config=config
                )
                for idx, (chunk, result) in enumerate(zip(chunks, chunk_results))
                if result
            ])
            
            # Generate overall summary
            summary = await file_processing_pipeline_generate_document_metadata_overall_summary(
                chunks=[chunk["text"] for chunk in chunks],
                filename=file.name,
                config=config
            )
            
            processed_files[file.name] = FileSummary(
                file_name=file.name,
                total_chunks=len(chunks),
                processing_method=config.processing_method,
                session_id=config.session_id,
                summary=summary.summary,
                key_points=summary.key_points,
                document_type=summary.document_type,
                metadata={
                    "themes": summary.themes,
                    "all_hashtags": summary.all_hashtags,
                    "key_questions": summary.key_questions
                }
            )
            
            if update_file_status_func:
                update_file_status_func(file.name, "✅ Processing complete")
                
        except Exception as e:
            logger.error(f"Error processing Excel {file.name}: {str(e)}")
            if update_file_status_func:
                update_file_status_func(file.name, f"❌ Error: {str(e)}")
    
    # Process all Excel files concurrently
    await asyncio.gather(*[process_single_excel(file) for file in files])
    return processed_files

async def process_csv_files(
    files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None
) -> Dict[str, FileSummary]:
    """Process CSV files concurrently."""
    processed_files = {}
    
    async def process_single_csv(file):
        try:
            if update_file_status_func:
                update_file_status_func(file.name, "🔍 Processing CSV file...")
            
            # Use ThreadPoolExecutor for pandas operations
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                df = await loop.run_in_executor(pool, pd.read_csv, file)
            
            chunks = []
            for idx, row in df.iterrows():
                text_chunk = f"Row {idx}: " + ", ".join([f"{col}: {val}" for col, val in row.items()])
                chunks.append({
                    "text": text_chunk,
                    "metadata": {
                        "row": idx,
                        "original_values": row.to_dict()
                    }
                })
            
            # Process chunks concurrently
            chunk_results = await asyncio.gather(*[
                file_processing_pipeline_step5_generate_metadata(
                    chunk["text"],
                    file.name,
                    idx,
                    config
                )
                for idx, chunk in enumerate(chunks)
            ])
            
            # Add chunks to unified collection
            await asyncio.gather(*[
                file_processing_pipeline_step6_add_unified_document_chunk(
                    source_name=file.name,
                    index=idx,
                    file_type="csv",
                    text_chunk=chunk["text"],
                    title=result.get("title", ""),
                    hashtags=result.get("hashtags", []),
                    hypothetical_questions=result.get("hypothetical_questions", []),
                    summary=result.get("summary", ""),
                    metadata={**chunk["metadata"], **result.get("metadata", {})},
                    config=config
                )
                for idx, (chunk, result) in enumerate(zip(chunks, chunk_results))
                if result
            ])
            
            # Generate overall summary
            summary = await file_processing_pipeline_generate_document_metadata_overall_summary(
                chunks=[chunk["text"] for chunk in chunks],
                filename=file.name,
                config=config
            )
            
            processed_files[file.name] = FileSummary(
                file_name=file.name,
                total_chunks=len(chunks),
                processing_method=config.processing_method,
                session_id=config.session_id,
                summary=summary.summary,
                key_points=summary.key_points,
                document_type=summary.document_type,
                metadata={
                    "themes": summary.themes,
                    "all_hashtags": summary.all_hashtags,
                    "key_questions": summary.key_questions
                }
            )
            
            if update_file_status_func:
                update_file_status_func(file.name, "✅ Processing complete")
                
        except Exception as e:
            logger.error(f"Error processing CSV {file.name}: {str(e)}")
            if update_file_status_func:
                update_file_status_func(file.name, f"❌ Error: {str(e)}")
    
    # Process all CSV files concurrently
    await asyncio.gather(*[process_single_csv(file) for file in files])
    return processed_files

async def process_other_files(
    files: List,
    config: ProcessingConfig,
    update_file_status_func: Optional[callable] = None,
    progress_bar: Optional[Any] = None
) -> Dict[str, FileSummary]:
    """Process other file types concurrently."""
    processed_files = {}
    
    async def process_single_file(file):
        try:
            result = await file_processing_pipeline_step4_process_document(
                file_data=file.getvalue(),
                filename=file.name,
                config=config,
                sem=asyncio.Semaphore(5),
                update_file_status_func=update_file_status_func,
                progress_bar=progress_bar
            )
            
            if result.success and result.document_info:
                # Process chunks concurrently
                await asyncio.gather(*[
                    file_processing_pipeline_step6_add_unified_document_chunk(
                        source_name=doc_info.source_name,
                        index=doc_info.index,
                        file_type="other",
                        text_chunk=doc_info.text_chunk,
                        title=doc_info.title,
                        hashtags=doc_info.hashtags,
                        hypothetical_questions=doc_info.hypothetical_questions,
                        summary=doc_info.summary,
                        metadata=doc_info.metadata,
                        config=config
                    )
                    for doc_info in result.document_info
                ])
                
                # Generate overall summary
                summary = await file_processing_pipeline_generate_document_metadata_overall_summary(
                    chunks=[doc.text_chunk for doc in result.document_info],
                    filename=file.name,
                    config=config
                )
                
                processed_files[file.name] = FileSummary(
                    file_name=file.name,
                    total_chunks=len(result.document_info),
                    processing_method=config.processing_method,
                    session_id=config.session_id,
                    summary=summary.summary,
                    key_points=summary.key_points,
                    document_type=summary.document_type,
                    metadata={
                        "themes": summary.themes,
                        "all_hashtags": summary.all_hashtags,
                        "key_questions": summary.key_questions
                    }
                )
                
                if update_file_status_func:
                    update_file_status_func(file.name, "✅ Processing complete")
                    
            else:
                if update_file_status_func:
                    update_file_status_func(file.name, f"❌ Error: {result.error}")
                logger.error(f"Error processing file {file.name}: {result.error}")
                
        except Exception as e:
            logger.error(f"Error processing file {file.name}: {str(e)}")
            if update_file_status_func:
                update_file_status_func(file.name, f"❌ Error: {str(e)}")
    
    # Process all files concurrently
    await asyncio.gather(*[process_single_file(file) for file in files])
    return processed_files

def initialize_session_state():
    """Initialize session state variables for document tracking."""
    if 'unified_documents' not in st.session_state:
        st.session_state['unified_documents'] = {}
    if 'document_store' not in st.session_state:
        st.session_state['document_store'] = []
    if 'all_unique_document_sources' not in st.session_state:
        st.session_state['all_unique_document_sources'] = set()
    if 'sem' not in st.session_state:
        st.session_state.sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    if 'openai_client' not in st.session_state:
        st.session_state.openai_client = AsyncOpenAI(
            api_key=st.secrets.get("OPENAI_API_KEY")
        )
