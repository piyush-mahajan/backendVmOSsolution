from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
# from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
import mysql.connector
from mysql.connector import Error
import os
from dotenv import load_dotenv
import uuid
from typing import Optional, List, Dict, Any
from azure.storage.blob import BlobServiceClient
import logging
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI(
    title="YouTube Transcription API",
    description="REST API for handling YouTube video transcription requests",
    version="1.0.0"
)

# Configure CORS to allow requests from your React app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Add middleware for allowed hosts
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"]  # Configure this properly in production
)

# Pydantic models for request/response validation
class TranscriptionRequest(BaseModel):
    youtube_url: HttpUrl
    email: str

class TranscriptionSegment(BaseModel):
    timestamp: str
    text: str
    start_seconds: int

class TranscriptionData(BaseModel):
    segments: List[TranscriptionSegment]

class TranscriptionResponse(BaseModel):
    request_id: str
    status: str
    transcription_url: Optional[str] = None
    transcription_data: Optional[TranscriptionData] = None
    is_complete: bool = False  # Field to indicate if processing is complete

# Database connection
def get_db_connection():
    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST"),
            user=os.getenv("MYSQL_USER"),
            password=os.getenv("MYSQL_PASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
            port=int(os.getenv("MYSQL_PORT", "3306"))
        )
        return connection
    except Error as e:
        logger.error(f"Error connecting to MySQL: {e}")
        raise HTTPException(status_code=500, detail="Database connection error")

# Azure Blob Storage configuration
blob_service_client = BlobServiceClient.from_connection_string(
    os.getenv("AZURE_STORAGE_CONNECTION_STRING")
)
container_name = os.getenv("AZURE_BLOB_CONTAINER_NAME")

# API Endpoints
@app.get("/api/health")
async def health_check():
    """Health check endpoint to verify API is running"""
    return {"status": "healthy", "version": "1.0.0"}

@app.post("/api/transcriptions", response_model=TranscriptionResponse)
async def request_transcription(request: TranscriptionRequest):
    """Submit a new transcription request"""
    request_id = str(uuid.uuid4())
    
    connection = get_db_connection()
    cursor = connection.cursor()
    
    try:
        # Insert new request
        insert_query = """
        INSERT INTO requests (request_id, youtube_url, email, status)
        VALUES (%s, %s, %s, 'Pending')
        """
        cursor.execute(insert_query, (request_id, str(request.youtube_url), request.email))
        connection.commit()
        
        return TranscriptionResponse(
            request_id=request_id,
            status="Pending",
            is_complete=False
        )
    
    except Error as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

@app.get("/api/transcriptions/{request_id}", response_model=TranscriptionResponse)
async def get_transcription_status(request_id: str):
    """Get the status of a transcription request"""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    
    try:
        # Get request status
        select_query = "SELECT * FROM requests WHERE request_id = %s"
        cursor.execute(select_query, (request_id,))
        result = cursor.fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Request not found")
        
        # Set is_complete flag if status is "Sent" and we have data
        is_complete = result["status"] == "Sent" and (result.get("transcription_url") is not None or result.get("transcription_text") is not None)
        
        transcription_data = None
        
        # If there's transcription_text, convert it to the required JSON format
        if result.get("transcription_text"):
            try:
                # Check if it's already in JSON format
                try:
                    existing_data = json.loads(result["transcription_text"])
                    if "segments" in existing_data:
                        transcription_data = existing_data
                except json.JSONDecodeError:
                    # It's a plain text, so parse and convert to JSON
                    segments = []
                    lines = result["transcription_text"].strip().split("\n")
                    
                    current_segment = {"timestamp": "00:00", "text": "", "start_seconds": 0}
                    
                    for line in lines:
                        # Simple parsing logic - this might need adjustment based on your text format
                        if line.strip() and ":" in line[:5]:  # Timestamp detection
                            # Save previous segment if it exists
                            if current_segment["text"]:
                                segments.append(current_segment.copy())
                            
                            # Parse new timestamp
                            parts = line.split(" ", 1)
                            timestamp = parts[0]
                            text = parts[1] if len(parts) > 1 else ""
                            
                            # Convert timestamp to seconds
                            minutes, seconds = map(int, timestamp.split(":"))
                            start_seconds = minutes * 60 + seconds
                            
                            current_segment = {
                                "timestamp": timestamp, 
                                "text": text,
                                "start_seconds": start_seconds
                            }
                        elif line.strip():
                            # Append to current segment text
                            current_segment["text"] += " " + line.strip()
                    
                    # Add the last segment
                    if current_segment["text"]:
                        segments.append(current_segment)
                    
                    transcription_data = {"segments": segments}
                    
                    # Update the database with the JSON format
                    update_query = "UPDATE requests SET transcription_text = %s WHERE request_id = %s"
                    cursor.execute(update_query, (json.dumps(transcription_data), request_id))
                    connection.commit()
            except Exception as e:
                logger.error(f"Error converting transcription to JSON: {e}")
                # If conversion fails, leave as None
                pass
        
        response = TranscriptionResponse(
            request_id=result["request_id"],
            status=result["status"],
            transcription_url=result.get("transcription_url"),
            transcription_data=transcription_data,
            is_complete=is_complete
        )
        
        return response
        
    except Error as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

@app.get("/api/transcriptions")
async def list_transcriptions(email: Optional[str] = None, skip: int = 0, limit: int = 10):
    """Get a list of transcription requests, optionally filtered by email"""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    
    try:
        if email:
            select_query = "SELECT * FROM requests WHERE email = %s ORDER BY created_at DESC LIMIT %s OFFSET %s"
            cursor.execute(select_query, (email, limit, skip))
        else:
            select_query = "SELECT * FROM requests ORDER BY created_at DESC LIMIT %s OFFSET %s"
            cursor.execute(select_query, (limit, skip))
            
        results = cursor.fetchall()
        
        # Format the results
        transcriptions = []
        for result in results:
            is_complete = result["status"] == "Sent" and (result.get("transcription_url") is not None or result.get("transcription_text") is not None)
            
            transcription_data = None
            if result.get("transcription_text"):
                try:
                    # Try to parse JSON data
                    json_data = json.loads(result["transcription_text"])
                    if "segments" in json_data:
                        transcription_data = json_data
                except:
                    # If it's not JSON, leave as None - we'll convert it when requested directly
                    pass
            
            transcriptions.append({
                "request_id": result["request_id"],
                "youtube_url": result["youtube_url"],
                "email": result["email"],
                "status": result["status"],
                "created_at": result.get("created_at").isoformat() if result.get("created_at") else None,
                "transcription_url": result.get("transcription_url"),
                "transcription_data": transcription_data,
                "is_complete": is_complete
            })
        
        return {"transcriptions": transcriptions, "count": len(transcriptions)}
        
    except Error as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

@app.delete("/api/transcriptions/{request_id}")
async def delete_transcription(request_id: str):
    """Delete a transcription request"""
    connection = get_db_connection()
    cursor = connection.cursor()
    
    try:
        # First check if the request exists
        select_query = "SELECT * FROM requests WHERE request_id = %s"
        cursor.execute(select_query, (request_id,))
        result = cursor.fetchone()
        
        if not result:
            raise HTTPException(status_code=404, detail="Request not found")
        
        # Delete the request
        delete_query = "DELETE FROM requests WHERE request_id = %s"
        cursor.execute(delete_query, (request_id,))
        connection.commit()
        
        return {"message": "Transcription request deleted successfully"}
        
    except Error as e:
        logger.error(f"Database error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

def start_server():
    """Start the FastAPI server"""
    host = os.getenv("YT_APP1_HOST", "127.0.0.1")
    port = int(os.getenv("YT_APP1_PORT", "3000"))
    logger.info(f"Starting server on http://{host}:{port}")
    
    import uvicorn
    uvicorn.run(
        "checkServer:app",
        host=host,
        port=port,
        log_level="info",
        reload=True
    )

if __name__ == "__main__":
    start_server()