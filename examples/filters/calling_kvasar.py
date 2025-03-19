import os
import json
import uuid
import openai
import requests
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

from utils.pipelines.main import get_last_user_message

# Configuration model
class PipelineConfig(BaseModel):
    class Valves(BaseModel):
        pipelines: List[str] = []
        priority: int = 0
          # Kvasar API Configuration
        kvasar_api_url: str = "https://kvasar.herokuapp.com/api/v1/search"
        openai_model: str = "gpt-4"
        auth0_token_url: str = "https://dev-k97g0lsy.eu.auth0.com/oauth/token"
        client_id: str = ""
        client_secret: str = ""
        audience: str = "https://kvasar.herokuapp.com/api/v1/"

         # OpenAI Configuration
        openai_model: str = "gpt-4"
        openai_api_key: str = ""
        debug: bool = False

    def __init__(self):
        self.type = "filter"
        self.name = "Kvasar API Pipeline"
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
                "client_id": os.getenv("KVASAR_CLIENT_ID", ""),
                "client_secret": os.getenv("KVASAR_CLIENT_SECRET", ""),
                "auth0_token_url": os.getenv("KVASAR_AUTH0_URL", "https://dev-k97g0lsy.eu.auth0.com/oauth/token"),
                "audience": os.getenv("KVASAR_AUDIENCE", "https://kvasar.herokuapp.com/api/v1/"),
                "kvasar_api_url": os.getenv("KVASAR_API_URL", "https://kvasar.herokuapp.com/api/v1/search"),
                "openai_model": os.getenv("OPENAI_MODEL", "gpt-4"),
                "debug": os.getenv("DEBUG_MODE", "false").lower() == "true",
                }
           )
        
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
        response = requests.post(self.valves.auth0_token_url, json=payload)
        response.raise_for_status()
        return response.json()["access_token"]

    def _generate_api_call(self, prompt: str) -> dict:
        """Convert natural language prompt to API call structure using OpenAI"""
        openai.api_key = self.valves.openai_api_key
        response = openai.ChatCompletion.create(
            model=self.valves.openai_model,
            messages=[{
                "role": "system",
                "content": "Convert user requests to Kvasar API calls. Respond ONLY with JSON containing: endpoint, method, body."
            }, {
                "role": "user", 
                "content": prompt
            }],
            temperature=0.3,
        )
        return json.loads(response.choices[0].message.content.strip())

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
            return {"error": str(e), "status_code": response.status_code}

    async def inlet(self, body: dict, user: Optional[dict] = None) -> dict:
        """Process user input to generate and execute API calls"""
        messages = body.get("messages", [])
        user_message = get_last_user_message(messages)

        if not user_message:
            return body

        try:
            # Generate API call structure
            api_call = self._generate_api_call(user_message)
            # Get authentication token
            token = self._get_auth_token()
            # Execute API call
            api_response = self._execute_api_call(api_call, token)
            
            # Format API response for LLM context
            api_context = (
                f"API Request: {json.dumps(api_call, indent=2)}\n"
                f"API Response: {json.dumps(api_response, indent=2)}"
            )
        except Exception as e:
            api_context = f"API Error: {str(e)}"

        # Append API context as system message
        messages.append({
            "role": "system",
            "content": api_context,
            "hidden": True  # Optional: Hide from user-facing chat
        })

        return {**body, "messages": messages}

    async def outlet(self, body: dict, user: Optional[dict] = None) -> dict:
        """Process LLM output if needed"""
        return body