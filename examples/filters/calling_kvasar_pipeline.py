"""
title: KVASAR Agile Filter
author: JordiMarti
date: 2025-03-19
version: 1.0
license: MIT
description: This pipeline integrates KVASAR Agile for automatic items creation 

"""
import os
import json
import uuid
from openai import OpenAI  # Changed import
import requests
from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional,  Union,Generator, Iterator,Dict, Any

from utils.pipelines.main import get_last_user_message

from logging import getLogger
logger = getLogger(__name__)
logger.setLevel("DEBUG")

def get_last_assistant_message_obj(messages: List[dict]) -> dict:
    for message in reversed(messages):
        if message["role"] == "assistant":
            return message
    return {}

# Configuration model
class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = []
        priority: int = 0
        # Kvasar API Configuration
        kvasar_api_url: str 
        openai_model: str 
        auth0_token_url: str 
        client_id: str 
        client_secret: str
        audience: str 

        # OpenAI Configuration
        openai_api_key: str = ""
        debug: bool = False

    def __init__(self):
       
        self.name = "Kvasar API Pipeline"
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
                "client_id": os.getenv("KVASAR_CLIENT_ID", ""),
                "client_secret": os.getenv("KVASAR_CLIENT_SECRET", ""),
                "auth0_token_url": os.getenv("KVASAR_AUTH0_URL", "https://dev-k97g0lsy.eu.auth0.com/oauth/token"),
                "audience": os.getenv("KVASAR_AUDIENCE", "https://kvasar.herokuapp.com//api/v1/"),
                "kvasar_api_url": os.getenv("KVASAR_API_URL", "https://kvasar.herokuapp.com/api/v1/items/"),
                "openai_model": os.getenv("OPENAI_MODEL", "gpt-4"),
                "debug": os.getenv("DEBUG_MODE", "false").lower() == "true",
            }
        )
        self.access_token = ""
        self.suppressed_logs = set()  # Initialize suppressed_logs
          # Initialize OpenAI client
         # Added client initialization
    
    def log(self, message: str, suppress_repeats: bool = False):
        """Logs messages to the terminal if debugging is enabled."""
        if self.valves.debug:
            if suppress_repeats:
                if message in self.suppressed_logs:
                    return
                self.suppressed_logs.add(message)
            print(f"[DEBUG] {message}")

    async def on_startup(self):
        print(f"Kvasar pipeline started: {__name__}")

    async def on_shutdown(self):
        print(f"Kvasar pipeline stopped: {__name__}")
        pass

    async def on_valves_updated(self):
        pass

    def _get_auth_token(self):
        """Retrieve authentication token from Auth0"""
        payload = {
            "client_id": self.valves.client_id,
            "client_secret": self.valves.client_secret,
            "audience": self.valves.audience,
            "grant_type": "client_credentials"
          }
    
        try:
            headers = { 'content-type': "application/json" }
            logger.info(f"payload: {payload}")
            
            response = requests.post(self.valves.auth0_token_url, json=payload,headers=headers)
            response.raise_for_status()
            token = response.json().get("access_token")
        
            if not token:
                logger.error("Auth0 response does not contain an access token: %s", response.json())
                raise ValueError("Failed to retrieve access token")
        
            logger.debug("Successfully retrieved access token")
            return token

        except requests.exceptions.RequestException as e:
            logger.error("Failed to get authentication token: %s", e, exc_info=True)
        raise

  

    def _execute_api_call(self, api_call: dict, token: str) -> dict:
        """Execute the generated API call against Kvasar API"""
        endpoint = api_call.get("endpoint", "")
        method = api_call.get("method", "GET").upper()
        body = api_call.get("body", {})

        url = f"{self.valves.kvasar_api_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, params=body)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=body)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=body)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            return {"error": str(e), "status_code": response.status_code} if response else {"error": str(e)}

    
    def pipe(self, user_message: str, model_id: str, 
           messages: List[dict], body: dict) -> Union[str, Generator, Iterator]:
        
        self.openai_client = OpenAI(api_key=self.valves.openai_api_key) 
        logger.debug(f"Processing Kvasar request: {user_message}")
        
        if body.get("title", False):
            return "(title generation disabled)"

        streaming = body.get("stream", False)
        context = ""
        dt_start = datetime.now()

        try:
            if streaming:
                yield from self.execute_kvasar_operation(user_message, dt_start)
            else:
                for chunk in self.execute_kvasar_operation(user_message, dt_start):
                    context += chunk
                return context
        except Exception as e:
            error_msg = f"Kvasar API Error: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def execute_kvasar_operation(self, command: str, dt_start: datetime) -> Generator:
        """Execute Kvasar API operation with streaming support"""
        try:
            # Generate API call structure using OpenAI
            response = self.openai_client.chat.completions.create(  # Updated API call
                model=self.valves.openai_model,
                messages=[{
                    "role": "system",
                    "content": "Convert this command to Kvasar API call. Respond ONLY with JSON containing: endpoint, method, body."
                }, {
                    "role": "user", 
                    "content": command
                }]
            )
            
            
            api_call = json.loads(response.choices[0].message.content)
            logger.info(f"Generated API call: {api_call}")
            
            # Execute API call
            token = self._get_auth_token()
            result = self._execute_api_call(api_call, token)
             # Handle error responses
            if "error" in result:
                yield f"## API Error\n```\n{result['error']}\nStatus Code: {result.get('status_code', 'Unknown')}\n```\n"
                return

            # Stream successful response
            yield f"## Operation Successful\n"
            yield f"**Endpoint**: {api_call['endpoint']}\n"
            yield f"**Method**: {api_call['method']}\n"
            yield "### Response Data\n```json\n"
            yield json.dumps(result, indent=2)
            yield "\n```\n"

        except requests.exceptions.RequestException as e:
            yield f"## Connection Error\n```\n{str(e)}\n```\n"
        except json.JSONDecodeError:
            yield "## Error: Invalid API response format\n"
        except Exception as e:
            logger.exception("Unexpected error in Kvasar operation")
            yield f"## Processing Error\n```\n{str(e)}\n```\n"