import os
import requests
import json
import openai
from typing import Literal, List, Optional
from blueprints.function_calling_blueprint import Pipeline as FunctionCallingBlueprint


class Pipeline(FunctionCallingBlueprint):
    class Valves(FunctionCallingBlueprint.Valves):
        # Add your custom parameters here
        # Kvasar API Configuration
        kvasar_api_url: str = "https://kvasar.herokuapp.com/api/v1"
        auth0_token_url: str = "https://dev-k97g0lsy.eu.auth0.com/oauth/token"
        client_id: str = ""
        client_secret: str = ""
        audience: str = "https://kvasar.herokuapp.com/api/v1/"
        
        # OpenAI Configuration
        openai_model: str = "gpt-4"
        openai_api_key: str = ""
        pass

    class Tools:
        def __init__(self, pipeline) -> None:
            self.pipeline = pipeline
            self._auth_token = None

        def _get_auth_token(self):
            """Retrieve and cache authentication token"""
            if not self._auth_token:
                payload = {
                    "client_id": self.pipeline.valves.client_id,
                    "client_secret": self.pipeline.valves.client_secret,
                    "audience": self.pipeline.valves.audience,
                    "grant_type": "client_credentials"
                }
                response = requests.post(self.pipeline.valves.auth0_token_url, json=payload)
                response.raise_for_status()
                self._auth_token = response.json()["access_token"]
            return self._auth_token

        def kvasar_api(
            self,
            command: str,
            operation_type: Literal["create", "read", "update", "delete", "search"] = "search"
        ) -> str:
            """
            Execute Kvasar API operations based on natural language commands. 
            Supports creating, reading, updating, deleting, and searching resources.

            :param command: Natural language description of the desired operation
            :param operation_type: Type of operation to perform (default: search)
            :return: API response or error message
            """
            try:
                # Generate API call structure using OpenAI
                openai.api_key = self.pipeline.valves.openai_api_key
                response = openai.ChatCompletion.create(
                    model=self.pipeline.valves.openai_model,
                    messages=[{
                        "role": "system",
                        "content": f"""Convert this command to Kvasar API call structure. 
                        Respond ONLY with JSON containing: endpoint, method, body.
                        Operation type: {operation_type}"""
                    }, {
                        "role": "user", 
                        "content": command
                    }],
                    temperature=0.3,
                )
                
                api_call = json.loads(response.choices[0].message.content.strip())
                token = self._get_auth_token()

                # Execute API call
                endpoint = api_call.get("endpoint", "")
                method = api_call.get("method", "GET").upper()
                body = api_call.get("body", {})
                
                url = f"{self.pipeline.valves.kvasar_api_url}{endpoint}"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }

                if method == "GET":
                    response = requests.get(url, headers=headers, params=body)
                elif method == "POST":
                    response = requests.post(url, headers=headers, json=body)
                elif method == "PUT":
                    response = requests.put(url, headers=headers, json=body)
                elif method == "DELETE":
                    response = requests.delete(url, headers=headers)
                else:
                    return "Unsupported HTTP method"

                response.raise_for_status()
                return json.dumps(response.json(), indent=2)

            except Exception as e:
                return f"API Error: {str(e)}"

    def __init__(self):
        super().__init__()
        self.name = "Kvasar API Pipeline"
        self.valves = self.Valves(
            **{
                **self.valves.model_dump(),
                "pipelines": ["*"],  # Connect to all pipelines
                "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
                "client_id": os.getenv("KVASAR_CLIENT_ID", ""),
                "client_secret": os.getenv("KVASAR_CLIENT_SECRET", ""),
                "auth0_token_url": os.getenv("KVASAR_AUTH0_URL", self.Valves.auth0_token_url.default),
                "audience": os.getenv("KVASAR_AUDIENCE", self.Valves.audience.default),
                "kvasar_api_url": os.getenv("KVASAR_API_URL", self.Valves.kvasar_api_url.default),
                "openai_model": os.getenv("OPENAI_MODEL", self.Valves.openai_model.default),
            },
        )
        self.tools = self.Tools(self)