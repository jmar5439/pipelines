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
import requests
from datetime import datetime
from typing import List, Optional, Union, Generator, Iterator, Dict, Any
from langgraph.graph import END 
from pydantic import BaseModel
from openai import OpenAI  # Changed import
from deepseek import DeepSeekAPI  # 

# from utils.pipelines.main import get_last_user_message

from logging import getLogger
logger = getLogger(__name__)
logger.setLevel("DEBUG")

# LangGraph imports
from langgraph.graph import StateGraph

def get_last_assistant_message_obj(messages: List[dict]) -> dict:
    for message in reversed(messages):
        if message["role"] == "assistant":
            return message
    return {}

class OpenAPISpec(BaseModel):
    endpoints: Dict[str, Dict[str, Dict]]
    base_url: str

class HashableState(dict):
    def __hash__(self):
        # Convert the dictionary items to a frozenset (which is hashable)
        return hash(frozenset(self.items()))

# Define a proper state schema for LangGraph that is immutable (hashable)
class PipelineState(BaseModel):
    command: str
    api_call: Optional[dict] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    output: Optional[str] = None
    next_state: Optional[str] = None

    model_config = {
        "frozen": True,  # Replaces the old Config class
        "extra": "forbid",
        "validate_assignment": True
    }

# Configuration model
class Pipeline:
    class Valves(BaseModel):
        pipelines: List[str] = []
        priority: int = 0
        openapi_spec_url: str = ""
        # Kvasar API Configuration
        kvasar_api_url: str 
        deepseek_model: str  # Changed to deepseek_model
        oauth_access_token: str
     
        # DeepSeek Configuration
        deepseek_api_key: str = ""  # Changed to deepseek_api_key
        debug: bool = False
        cache_spec_minutes: int = 60

    def __init__(self):
        self.name = "Kvasar API Pipeline 4 Deepseek"
        self.spec_last_fetched: Optional[datetime] = None
        self.openapi_spec: Optional[OpenAPISpec] = None
        self.valves = self.Valves(
            **{
                "pipelines": ["*"],
                "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", ""),
                "oauth_access_token":os.getenv("OAUTH_ACCESS_TOKEN",""),
                "kvasar_api_url": os.getenv("KVASAR_API_URL", ""),
                 "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                "openapi_spec_url": os.getenv("KVASAR_OPENAPI_URL", ""),
                "debug": os.getenv("DEBUG_MODE", "false").lower() == "true"
            }
        )
        self.access_token = ""
        self.suppressed_logs = set()
        # OpenAI client initialization will be done in the pipe() method

    def log(self, message: str, suppress_repeats: bool = False):
        """Logs messages to the terminal if debugging is enabled."""
        message_str = str(message)  # Ensure message is a string
        if self.valves.debug:
            if suppress_repeats:
                if message_str in self.suppressed_logs:
                    return
                self.suppressed_logs.add(message_str)
            print(f"[DEBUG] {message_str}")

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
            except requests.exceptions.RequestException as e:
                logger.error("Failed to refresh OpenAPI spec: %s", str(e))
                return {"error": "Failed to fetch OpenAPI Spec"}
            except Exception as e:
                logger.error("Failed to refresh OpenAPI spec: %s", str(e))
                if not self.openapi_spec:
                    raise RuntimeError("Critical: Failed to fetch initial API spec")
                
    def spec_needs_refresh(self) -> bool:
        if not self.spec_last_fetched or  len(self.openapi_spec.endpoints) == 0:
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

    async def on_valves_updated(self):
        pass

    

    def _execute_api_call(self, api_call: dict, token: str) -> dict:
        """Execute the generated API call against Kvasar API"""
        endpoint = api_call.get("endpoint", "")
        method = api_call.get("method", "GET").upper()
        body = api_call.get("body", {})

        url = f"{self.valves.kvasar_api_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
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
            if response.text:
                return response.json()
            else:
                return {"error": "Empty response from server"}
            # return response.json()
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

    # ----------------------------
    # LangGraph State Machine Nodes
    # ----------------------------

    def node_generate_api_call(self, state: PipelineState) -> PipelineState:  # Changed type hint
        """
        State 1: Generate API Call.
        Uses OpenAI to generate a structured API call from the user command.
        """
        command = state.command  # Direct attribute access
        self.log(f"Generating API call for command: {command}")
        try:
            create_args = {
                "model": self.valves.deepseek_model,
                "messages": self._generate_api_call_prompt(command),
                "temperature": 0.1
            }
            supports_json = any(m in self.valves.deepseek_model.lower() 
                             for m in ['deepseek-chat', 'deepseek-coder'])
            if supports_json:
                create_args["response_format"] = {"type": "json_object"}
                
            
            raw_response = self.deepseek_client.chat_completion(**create_args)

            #raw_response = response.choices[0].message.content
        
            # Parse response
            if isinstance(raw_response, dict):
                api_call = raw_response
            else:
                try:
                    api_call = json.loads(raw_response)
                except json.JSONDecodeError:
                    json_str = raw_response.strip('` \n').replace('json\n', '')
                    api_call = json.loads(json_str)
                
            self.log(f"Generated API call: {api_call}")
            return PipelineState(
                **state.model_dump(exclude={'api_call', 'error', 'next_state'}), 
                api_call=api_call,
                next_state="execute_api_call"
            )
        except Exception as e:
            return PipelineState(
                **state.model_dump(exclude={'error', 'next_state'}),
                error=f"API generation error: {str(e)}",
                next_state="handle_error"
            )

    def node_execute_api_call(self, state: PipelineState) -> PipelineState:  # Changed type hint
        """
        State 2: Execute API Call.
        Executes the API call and stores the result.
        """
        self.log("Executing API call")
        api_call = state.api_call  # Direct attribute access
        if not api_call:
            return PipelineState(
                 **state.model_dump(exclude={'error', 'next_state'}),
                error="No API call generated",
                next_state="handle_error"
            )

        # Validate API call against the OpenAPI spec
        if not self._validate_api_call(api_call):
            return PipelineState(
                 **state.model_dump(exclude={'output', 'next_state'}),
                error="Invalid API call structure",
                next_state="handle_error"
            )

        try:
            token = self.valves.oauth_access_token
            result = self._execute_api_call(api_call, token)
            if "error" in result:
                return PipelineState(
                     **state.model_dump(exclude={'error', 'next_state'}),
                    error=f"API Error: {result['error']}",
                    next_state="handle_error"
                )
            return PipelineState(
                **state.model_dump(exclude={'result', 'next_state'}),
                result=result,
                next_state="format_response"
            )
        except Exception as e:
            return PipelineState(
                 **state.model_dump(exclude={'error', 'next_state'}),
                error=f"Execution error: {str(e)}",
                next_state="handle_error"
            )


    def node_format_response(self, state: PipelineState) -> PipelineState:
        """
        State 3: Format Response for OpenWeb UI.
        Structures the response with interactive elements and proper JSON formatting.
        """
        self.log("Formatting API response for OpenWeb UI")
        try:
            # Base response structure
            formatted = {
                "type": "response",
                "elements": [
                    {
                        "type": "header",
                        "content": "✅ Operation Successful",
                        "level": 2
                    },
                    {
                        "type": "key_value",
                        "data": {
                            "Endpoint": f"`{state.api_call.get('endpoint', '')}`",
                            "Method": f"`{state.api_call.get('method', '').upper()}`"
                        }
                    }
                ]
            }

            # Add parameters if present
            if state.api_call.get('parameters'):
                formatted["elements"].append({
                    "type": "code_block",
                    "title": "Request Parameters",
                    "content": json.dumps(state.api_call['parameters'], indent=2),
                    "language": "json"
                })

            # Add response data with dynamic formatting
            response_content = {
                "type": "code_block",
                "title": "API Response",
                "content": json.dumps(state.result, indent=2),
                "language": "json"
            }

            if isinstance(state.result, list):
                response_content["metadata"] = {
                    "item_count": len(state.result),
                    "summary_fields": list(state.result[0].keys()) if state.result else []
                }
            
            formatted["elements"].append(response_content)

            # Convert to OpenWeb UI's expected string format
            output = json.dumps(formatted, indent=2)

            return PipelineState(
                **state.model_dump(exclude={'output', 'next_state'}),
                output=output,
                next_state="complete"
            )
        except Exception as e:
            return PipelineState(
                **state.model_dump(exclude={'error', 'next_state'}),
                error=f"Formatting error: {str(e)}",
                next_state="handle_error"
            )

    def node_handle_error(self, state: PipelineState) -> PipelineState:
        self.log("Handling error")
        error_message = state.error or "Unknown error"
        
        # Attempt to get debugging suggestions from DeepSeek
        try:
            raw_suggestion_response = self.deepseek_client.chat_completion(
                model=self.valves.deepseek_model,
                messages=[
                    {"role": "system", "content": "You are an assistant providing debugging tips. Always respond in JSON format with a 'suggestions' key."},
                    {"role": "user", "content": f"I encountered this error: {error_message}\nHow can I resolve it? Provide suggestions in json format."}
                ],
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            self.log(f'API Response: {raw_suggestion_response}')
            # First check if response structure is valid
            if isinstance(raw_suggestion_response, dict):
                try:
                    # Extract JSON string from message content
                    content_str = raw_suggestion_response['choices'][0]['message']['content']
                    
                    # Parse the JSON string to Python dict
                    import json  # Make sure to import json at the top of your file
                    content_dict = json.loads(content_str)
                    
                    # Extract suggestions from parsed JSON
                    suggestions = content_dict.get('suggestions', "No suggestions provided.")
                except (KeyError, IndexError, json.JSONDecodeError) as parse_error:
                    suggestions = f"Failed to parse response: {str(parse_error)}"
                except Exception as e:
                    suggestions = f"Unexpected error: {str(e)}"
            else:
                suggestions = "Response format invalid"
                
        except Exception as deepseek_error:
            suggestions = f"Failed to get suggestions from DeepSeek: {str(deepseek_error)}"
        
        return PipelineState(
            **state.model_dump(exclude={'output', 'next_state', 'error'}),
            output=f"## Error Occurred\n```\n{error_message}\n```\n\nSuggestions:\n```\n{suggestions}\n```",
            next_state="complete"
        )

    def _validate_api_call(self, api_call: dict) -> bool:
        """Validate generated call against OpenAPI spec"""
        if not self.openapi_spec:
            return True  # Skip validation if no spec available
            
        path = api_call.get('endpoint', '').split('?')[0]
        method = api_call.get('method', '').upper()
        for spec_path in self.openapi_spec.endpoints.keys():
            if self._path_matches_template(path, spec_path):
                if method in self.openapi_spec.endpoints[spec_path]:
                    return True
        logger.warning("Validation failed for %s %s", method, path)
        return False

    def _path_matches_template(self, actual_path: str, template_path: str) -> bool:
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

    # ----------------------------
    # Pipeline entry point using LangGraph
    # ----------------------------
    def execute_with_langgraph(self, command: str) -> str:
        # Initialize graph
        graph = StateGraph(PipelineState)
        
        # Add nodes using corrected methods
        graph.add_node("generate_api_call", self.node_generate_api_call)
        graph.add_node("execute_api_call", self.node_execute_api_call)
        graph.add_node("format_response", self.node_format_response)
        graph.add_node("handle_error", self.node_handle_error)

        # Set proper entry point
        graph.set_entry_point("generate_api_call")

        # Add conditional edges
        graph.add_conditional_edges(
            "generate_api_call",
            lambda state: "handle_error" if (state.error if isinstance(state, PipelineState) else state.get('error')) else "execute_api_call"
        )
        
        graph.add_conditional_edges(
            "execute_api_call",
            lambda state: "handle_error" if (state.error if isinstance(state, PipelineState) else state.get('error')) else "format_response"
        )

        # Connect terminal nodes
        graph.add_edge("format_response", END)
        graph.add_edge("handle_error", END)

        # Compile and execute
        app = graph.compile()
        final_state = app.invoke(PipelineState(command=command))
        
        # Handle both Pydantic model and dict access
        if isinstance(final_state, PipelineState):
            return final_state.output or "No output generated"
        return final_state.get('output', "No output generated")





    # ----------------------------
    # Existing pipe() function modified to use LangGraph
    # ----------------------------
    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> Union[str, Generator, Iterator]:
        self.deepseek_client = DeepSeekAPI(api_key=self.valves.deepseek_api_key)

            # Check that the OpenAPI spec is loaded and has endpoints.
        if not self.openapi_spec or len(self.openapi_spec.endpoints) == 0:
            logger.error("No endpoints loaded. Attempting to refresh OpenAPI spec...")
            try:
                # Try to refresh the spec (if running in a synchronous context, you might need to run it via asyncio)
                import asyncio
                asyncio.run(self.refresh_openapi_spec())
            except Exception as e:
                logger.error("Error during spec refresh: %s", str(e))
            
            if not self.openapi_spec or len(self.openapi_spec.endpoints) == 0:
                error_msg = "Critical Error: No endpoints available in OpenAPI spec after refresh."
                logger.error(error_msg)
                return error_msg  # Alternatively, you can raise an exception here

        logger.info("KVASAR pipeline started with %d endpoints loaded", 
                    len(self.openapi_spec.endpoints) if self.openapi_spec else 0)
        logger.debug(f"Processing Kvasar request: {user_message}")

        dt_start = datetime.now()
        if body.get("title", False):
            return "(title generation disabled)"
        try:
            # Use the LangGraph state machine to process the command
            output = self.execute_with_langgraph(user_message)
            return output
        except Exception as e:
            error_msg = f"Kvasar API Error: {str(e)}"
            logger.error(error_msg)
            return error_msg

# Example usage:
if __name__ == "__main__":
    pipeline = Pipeline()
    # Ensure the OpenAPI spec is loaded; in production, on_startup() would be awaited.
    try:
        import asyncio
        asyncio.run(pipeline.refresh_openapi_spec())
    except Exception as e:
        logger.error("Error during startup: %s", str(e))
    # Simulate a user command
    command = "Create a new agile item for sprint backlog"
    result = pipeline.pipe(command, model_id="gpt-4", messages=[], body={})
    print(result)
