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

class OpenAPISpec(BaseModel):
    endpoints: Dict[str, Dict[str, Dict]]
    base_url: str

# Configuration model
class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = []
        priority: int = 0
        openapi_spec_url: str = "https://kvasar.herokuapp.com/v3/api-docs"
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
        cache_spec_minutes: int = 60
        # spec_last_fetched: datetime

    def __init__(self):
       
        self.name = "Kvasar API Pipeline 2"
        self.spec_last_fetched: Optional[datetime] = None
        self.openapi_spec: Optional[OpenAPISpec] = None
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
                "client_id": os.getenv("KVASAR_CLIENT_ID", ""),
                "client_secret": os.getenv("KVASAR_CLIENT_SECRET", ""),
                "auth0_token_url": os.getenv("KVASAR_AUTH0_URL", "https://dev-k97g0lsy.eu.auth0.com/oauth/token"),
                "audience": os.getenv("KVASAR_AUDIENCE", "https://kvasar.herokuapp.com//api/v1/"),
                "kvasar_api_url": os.getenv("KVASAR_API_URL", "https://kvasar.herokuapp.com"),
                "openai_model": os.getenv("OPENAI_MODEL", "gpt-4"),
                "openapi_spec_url": os.getenv("KVASAR_OPENAPI_URL", "https://kvasar.herokuapp.com/v3/api-docs"),
                "debug": os.getenv("DEBUG_MODE", "false").lower() == "true",
            }
        )
        self.access_token = ""
        self.suppressed_logs = set()  # Initialize suppressed_logs
        #self.openapi_spec: Optional[OpenAPISpec] = None
        #self.spec_last_fetched: Optional[datetime] = None
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
        await self.refresh_openapi_spec()
        
        
    async def refresh_openapi_spec(self):
        """Fetch and cache OpenAPI specification with expiration"""
        if self.spec_needs_refresh():
            try:
                response = requests.get(self.valves.openapi_spec_url, timeout=10)
                response.raise_for_status()
                spec_data = response.json()
                
                endpoints = {}
                for path, methods in spec_data.get('paths', {}).items():
                    endpoints[path] = {
                        method.upper(): {
                            'summary': details.get('summary', ''),
                            'parameters': details.get('parameters', []),
                            'required': [p['name'] for p in details.get('parameters', []) 
                                       if p.get('required', False)]
                        }
                        for method, details in methods.items()
                    }

                self.openapi_spec = OpenAPISpec(
                    endpoints=endpoints,
                    base_url=spec_data.get('servers', [{}])[0].get('url', self.valves.kvasar_api_url)
                )
                self.spec_last_fetched = datetime.now()
                logger.info("Successfully updated OpenAPI spec with %d endpoints", len(endpoints))
                
            except Exception as e:
                logger.error("Failed to refresh OpenAPI spec: %s", str(e))
                if not self.openapi_spec:
                    raise RuntimeError("Critical: Failed to fetch initial API spec")
                
    def spec_needs_refresh(self) -> bool:
        if not self.spec_last_fetched:
            return True
        delta = datetime.now() - self.spec_last_fetched
        return delta.total_seconds() > self.valves.cache_spec_minutes * 60
    
    def _format_endpoints_prompt(self) -> str:
        """Convert OpenAPI spec to natural language prompt"""
        if not self.openapi_spec:
            return "No API endpoints information available"
        
        prompt = ["Available API endpoints:"]
        for path, methods in self.openapi_spec.endpoints.items():
            for method, details in methods.items():
                prompt.append(
                    f"- {method} {path}: {details['summary']}"
                    f" | Parameters: {', '.join(details['required']) or 'None'}"
                )
        return "\n".join(prompt)
                
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

    def _generate_api_call_prompt(self, user_command: str) -> List[dict]:
        base_prompt = {
            "role": "system",
            "content": f"""You are an API call generator. Convert user requests to Kvasar API calls.

        {self._format_endpoints_prompt()}

        Respond ONLY with JSON containing:
        - endpoint: Full path with parameters (e.g., /items/{{id}})
        - method: HTTP verb
        - body: JSON object for request body (if needed)
        - parameters: Path/query parameters (key-value pairs)

        Example:
        {{ "endpoint": "/items/123", "method": "GET", "body": {{}}, "parameters": {{}} }}
        """
        }
        return [
            base_prompt,
            {"role": "user", "content": user_command}
        ]
    def pipe(self, user_message: str, model_id: str, 
           messages: List[dict], body: dict) -> Union[str, Generator, Iterator]:
        
        self.openai_client = OpenAI(api_key=self.valves.openai_api_key) 
        ## self.refresh_openapi_spec()
        logger.info("KVASAR pipeline started with %d endpoints loaded", 
                   len(self.openapi_spec.endpoints) if self.openapi_spec else 0)
        logger.debug(f"Processing Kvasar request: {user_message}")

        dt_start = datetime.now()
        
        if body.get("title", False):
            return "(title generation disabled)"

        try:
            
            return self.execute_kvasar_operation(user_message,dt_start)
        except Exception as e:
            error_msg = f"Kvasar API Error: {str(e)}"
            logger.error(error_msg)
            return error_msg

    def execute_kvasar_operation(self, command: str, dt_start: datetime) -> Generator:
        """Execute Kvasar API operation with streaming support"""
        try:
            # Check model compatibility
            supports_json = any(m in self.valves.openai_model.lower() 
                          for m in ['turbo-preview', '0125', '1106'])
            api_call_prompt = self._generate_api_call_prompt(command)
            

             # Generate structured API call
            create_args = {
                "model": self.valves.openai_model,
                "messages": self._generate_api_call_prompt(command),
                "temperature": 0.1
            }
            if supports_json:
                create_args["response_format"] = {"type": "json_object"}

            # Generate structured API call
            response = self.openai_client.chat.completions.create(**create_args)

            # Handle different response formats
            raw_response = response.choices[0].message.content
            
            try:
                api_call = json.loads(raw_response)
            except json.JSONDecodeError:
            # Fallback: Try to extract JSON from markdown
                json_str = raw_response.strip('` \n').replace('json\n', '')
                api_call = json.loads(json_str)
            
            logger.info(f"Generated API call: {api_call}")
            
            
            # Validate against known endpoints
            if not self._validate_api_call(api_call):
                yield "Error: Invalid API call structure"
                return

            
            # Execute API call
            token = self._get_auth_token()
            result = self._execute_api_call(api_call, token)
             # Handle error responses
            if "error" in result:
                yield f"## API Error\n```\n{result['error']}\nStatus Code: {result.get('status_code', 'Unknown')}\n```\n"
                return

            yield from self._format_response(api_call, result)

           
        except requests.exceptions.RequestException as e:
            yield f"## Connection Error\n```\n{str(e)}\n```\n"
        except json.JSONDecodeError:
            yield "## Error: Invalid API response format\n"
        except Exception as e:
            logger.exception("Unexpected error in Kvasar operation")
            yield f"## Processing Error\n```\n{str(e)}\n```\n"

    def _path_matches_template(self, actual_path: str, template_path: str) -> bool:
        # Simple path parameter matching (e.g., /items/{id} vs /items/123)
        actual_parts = actual_path.strip('/').split('/')
        template_parts = template_path.strip('/').split('/')
        
        if len(actual_parts) != len(template_parts):
            return False
            
        for a, t in zip(actual_parts, template_parts):
            if t.startswith('{') and t.endswith('}'):
                continue
            if a != t:
                return False
        return True

    def _format_response(self, api_call: dict, result: dict) -> Generator:
        """Stream formatted response to client"""
        yield f"## Operation Successful\n"
        yield f"**Endpoint**: {api_call['endpoint']}\n"
        yield f"**Method**: {api_call['method']}\n"
        
        if api_call.get('parameters'):
            yield "### Parameters\n```json\n"
            yield json.dumps(api_call['parameters'], indent=2)
            yield "\n```\n"
            
        yield "### Response Data\n```json\n"
        yield json.dumps(result, indent=2)
        yield "\n```\n"

    def _validate_api_call(self, api_call: dict) -> bool:
        """Validate generated call against OpenAPI spec"""
        if not self.openapi_spec:
            return True  # Skip validation if no spec available
            
        path = api_call.get('endpoint', '').split('?')[0]
        method = api_call.get('method', '').upper()
        
        # Find matching path template
        for spec_path in self.openapi_spec.endpoints.keys():
            if self._path_matches_template(path, spec_path):
                if method in self.openapi_spec.endpoints[spec_path]:
                    return True
        logger.warning("Validation failed for %s %s", method, path)
        return False