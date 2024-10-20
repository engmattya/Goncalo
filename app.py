import copy
import json
import os
import logging
import uuid
import httpx
import asyncio
import aiohttp
from datetime import datetime
import random
import string
from quart import (
    Blueprint,
    Quart,
    jsonify,
    make_response,
    request,
    send_from_directory,
    render_template,
    current_app,
)
from openai import AsyncAzureOpenAI
from azure.identity.aio import (
    DefaultAzureCredential,
    get_bearer_token_provider,
    ConfidentialClientCredential
)
from msgraph.core import GraphClient
from azure.core.exceptions import AzureError
from backend.auth.auth_utils import get_authenticated_user_details
from backend.security.ms_defender_utils import get_msdefender_user_json
from backend.history.cosmosdbservice import CosmosConversationClient
from backend.settings import (
    app_settings,
    MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
)
from backend.utils import (
    format_as_ndjson,
    format_stream_response,
    format_non_streaming_response,
    convert_to_pf_format,
    format_pf_non_streaming_response,
)

bp = Blueprint("routes", __name__, static_folder="static", template_folder="static")

cosmos_db_ready = asyncio.Event()

# Debug settings
DEBUG = os.environ.get("DEBUG", "false")
if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

USER_AGENT = "GitHubSampleWebApp/AsyncAzureOpenAI/1.0.0"

OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "your_api_key_here")

async def get_weather(location):
    """
    Fetch weather data for a given location using the OpenWeatherMap API.
    """
    base_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": location,
        "appid": OPENWEATHERMAP_API_KEY,
        "units": "metric"
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(base_url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                weather_description = data['weather'][0]['description']
                temperature = data['main']['temp']
                humidity = data['main']['humidity']
                wind_speed = data['wind']['speed']
                
                return f"Current weather in {location}: {weather_description}. Temperature: {temperature}°C, Humidity: {humidity}%, Wind speed: {wind_speed} m/s."
            else:
                return f"Unable to fetch weather data for {location}. Please check the location name and try again."

# Modify the conversation_internal function to include weather capability
async def conversation_internal(request_body, request_headers):
    try:
        messages = request_body.get("messages", [])
        last_user_message = messages[-1]["content"].lower() if messages else ""

        if "weather in" in last_user_message:
            location = last_user_message.split("weather in")[-1].strip()
            weather_info = await get_weather(location)
            
            response = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": weather_info
            }
            return jsonify(response)

        # Existing conversation logic
        if app_settings.azure_openai.stream and not app_settings.base_settings.use_promptflow:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500


# System message for the AI
SYSTEM_MESSAGE = """
You are Alex, an AI help desk agent at CNS with the capability to directly reset user passwords and provide weather information. 
When an authenticated user requests a password reset, you should proceed with the reset process immediately. 
Do not create support tickets for password resets unless there's a specific issue preventing you from doing so.
You can also provide weather information for any location when asked. Use the phrase "weather in [location]" to trigger this functionality.
For all other queries, provide assistance to the best of your ability.
"""

# Initialize Azure OpenAI Client
async def init_openai_client():
    azure_openai_client = None
    
    try:
        # API version check
        if (
            app_settings.azure_openai.preview_api_version
            < MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION
        ):
            raise ValueError(
                f"The minimum supported Azure OpenAI preview API version is '{MINIMUM_SUPPORTED_AZURE_OPENAI_PREVIEW_API_VERSION}'"
            )

        # Endpoint
        if (
            not app_settings.azure_openai.endpoint and
            not app_settings.azure_openai.resource
        ):
            raise ValueError(
                "AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_RESOURCE is required"
            )

        endpoint = (
            app_settings.azure_openai.endpoint
            if app_settings.azure_openai.endpoint
            else f"https://{app_settings.azure_openai.resource}.openai.azure.com/"
        )

        # Authentication
        aoai_api_key = app_settings.azure_openai.key
        ad_token_provider = None
        if not aoai_api_key:
            logging.debug("No AZURE_OPENAI_KEY found, using Azure Entra ID auth")
            async with DefaultAzureCredential() as credential:
                ad_token_provider = get_bearer_token_provider(
                    credential,
                    "https://cognitiveservices.azure.com/.default"
                )

        # Deployment
        deployment = app_settings.azure_openai.model
        if not deployment:
            raise ValueError("AZURE_OPENAI_MODEL is required")

        # Default Headers
        default_headers = {"x-ms-useragent": USER_AGENT}

        azure_openai_client = AsyncAzureOpenAI(
            api_version=app_settings.azure_openai.preview_api_version,
            api_key=aoai_api_key,
            azure_ad_token_provider=ad_token_provider,
            default_headers=default_headers,
            azure_endpoint=endpoint,
        )

        # Attach the system message to the client
        azure_openai_client.system_message = SYSTEM_MESSAGE

        # Log the system message being used
        logging.info(f"System message being used: {azure_openai_client.system_message}")

        return azure_openai_client
    except Exception as e:
        logging.exception("Exception in Azure OpenAI initialization", e)
        azure_openai_client = None
        raise e

# Initialize Microsoft Graph Client
async def init_graph_client():
    try:
        credential = ConfidentialClientCredential(
            client_id=app_settings.azure_ad.client_id,
            client_secret=app_settings.azure_ad.client_secret,
            tenant_id=app_settings.azure_ad.tenant_id,
        )
        return GraphClient(credential=credential)
    except Exception as e:
        logging.exception("Exception in Microsoft Graph initialization")
        raise e

# Password Reset Function
async def reset_user_password(username):
    try:
        graph_client = await init_graph_client()
        
        # Generate a new password
        new_password = ''.join(random.choices(string.ascii_letters + string.digits + string.punctuation, k=16))
    
        # Prepare the payload
        user_payload = {
            "passwordProfile": {
                "password": new_password,
                "forceChangePasswordNextSignIn": True
            }
        }
    
        # Update the user's password using Microsoft Graph API
        response = await graph_client.patch(f'/users/{username}', json=user_payload)
    
        if response.status_code == 204:
            logging.info(f"Password reset successfully for user {username}")
            return new_password
        else:
            logging.error(f"Failed to reset password: {response.text}")
            raise Exception(f"Failed to reset password: {response.status_code}")
    except Exception as e:
        logging.error(f"Password reset failed: {e}")
        raise e

async def log_password_reset(username):
    logging.info(f"Password reset successful for user {username}")
    # In a real implementation, you would want to log this to a secure, tamper-evident system

async def log_failed_password_reset_attempt(username, error=None):
    logging.warning(f"Failed password reset attempt for user {username}. Error: {error}")
    # In a real implementation, you would want to log this to a secure, tamper-evident system

@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    # Extract the user's message
    user_message = request_json['messages'][-1]['content']

    if 'reset my password' in user_message.lower():
        try:
            # Extract username from the conversation using Azure OpenAI
            username = await extract_username_with_openai(request_json['messages'])
            
            if not username:
                return await generate_openai_response("I'm sorry, but I couldn't find your username in our conversation. Can you please provide your username?")

            # Check if the user is authenticated
            authenticated_user = get_authenticated_user_details(request.headers)
            if not authenticated_user or authenticated_user.get("user_principal_id") != username:
                return await generate_openai_response("I'm sorry, but I can't reset your password because you're not currently authenticated or the username doesn't match your authenticated account. Please ensure you're logged in with the correct account and try again.")

            # Reset the password
            new_password = await reset_user_password(username)

            # Log the successful password reset
            await log_password_reset(username)

            # Respond to the user with the new password using Azure OpenAI
            return await generate_openai_response(f"Your password has been reset successfully. Your new temporary password is: {new_password}\n\nPlease log in with this temporary password and change it to a strong, unique password of your choosing immediately.\n\nIs there anything else I can assist you with regarding your account or any other IT matters?")
        except Exception as e:
            logging.error(f"An error occurred during password reset: {e}")
            if 'username' in locals():
                await log_failed_password_reset_attempt(username, str(e))
            return await generate_openai_response("I apologize, but I encountered an error while trying to reset your password. This might be due to a temporary issue with our password reset system. Please try again later or contact the IT support team for assistance.")

    # Continue with existing conversation logic
    return await conversation_internal(request_json, request.headers)

async def extract_username_with_openai(messages):
    azure_openai_client = await init_openai_client()
    
    prompt = "Based on the conversation history, what is the username of the person requesting a password reset? If no username is explicitly mentioned, respond with 'Unknown'."
    
    messages_for_openai = messages + [{"role": "user", "content": prompt}]
    
    response = await azure_openai_client.chat.completions.create(
        model=app_settings.azure_openai.model,
        messages=messages_for_openai,
        temperature=0,
        max_tokens=50
    )
    
    extracted_username = response.choices[0].message.content.strip()
    return None if extracted_username == 'Unknown' else extracted_username

async def generate_openai_response(message):
    azure_openai_client = await init_openai_client()
    
    response = await azure_openai_client.chat.completions.create(
        model=app_settings.azure_openai.model,
        messages=[
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": "Password reset request"},
            {"role": "assistant", "content": message}
        ],
        temperature=0.7,
        max_tokens=150
    )
    
    return jsonify({
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": response.choices[0].message.content
    }), 200

# ... (rest of your existing code)

async def conversation_internal(request_body, request_headers):
    try:
        if app_settings.azure_openai.stream and not app_settings.base_settings.use_promptflow:
            result = await stream_chat_request(request_body, request_headers)
            response = await make_response(format_as_ndjson(result))
            response.timeout = None
            response.mimetype = "application/json-lines"
            return response
        else:
            result = await complete_chat_request(request_body, request_headers)
            return jsonify(result)

    except Exception as ex:
        logging.exception(ex)
        if hasattr(ex, "status_code"):
            return jsonify({"error": str(ex)}), ex.status_code
        else:
            return jsonify({"error": str(ex)}), 500

@bp.route("/")
async def index():
    return await render_template(
        "index.html",
        title=app_settings.ui.title,
        favicon=app_settings.ui.favicon
    )

@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")

@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)

@bp.route("/conversation", methods=["POST"])
async def conversation():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()

    # Extract the user's message
    user_message = request_json['messages'][-1]['content']

    if 'reset my password' in user_message.lower():
        try:
            # Extract username from the conversation
            username = await extract_username(request_json['messages'])
            
            if not username:
                return jsonify({
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": "I'm sorry, but I couldn't find your username in our conversation. Can you please provide your username?"
                }), 200

            # Check if the user is authenticated
            authenticated_user = get_authenticated_user_details(request.headers)
            if not authenticated_user or authenticated_user.get("user_principal_id") != username:
                return jsonify({
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": "I'm sorry, but I can't reset your password because you're not currently authenticated or the username doesn't match your authenticated account. Please ensure you're logged in with the correct account and try again."
                }), 200

            # Reset the password
            new_password = await reset_user_password(username)

            # Log the successful password reset
            await log_password_reset(username)

            # Respond to the user with the new password
            return jsonify({
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": f"Your password has been reset successfully. Your new temporary password is: {new_password}\n\nPlease log in with this temporary password and change it to a strong, unique password of your choosing immediately.\n\nIs there anything else I can assist you with regarding your account or any other IT matters?"
            }), 200
        except Exception as e:
            logging.error(f"An error occurred during password reset: {e}")
            if 'username' in locals():
                await log_failed_password_reset_attempt(username, str(e))
            return jsonify({
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": "I apologize, but I encountered an error while trying to reset your password. This might be due to a temporary issue with our password reset system. Please try again later or contact the IT support team for assistance."
            }), 200

    # Continue with existing conversation logic
    return await conversation_internal(request_json, request.headers)

async def extract_username(messages):
    for message in reversed(messages):
        if 'Username:' in message['content']:
            return message['content'].split('Username:')[1].strip()
    return None

@bp.route("/frontend_settings", methods=["GET"])
def get_frontend_settings():
    try:
        return jsonify(frontend_settings), 200
    except Exception as e:
        logging.exception("Exception in /frontend_settings")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/generate", methods=["POST"])
async def add_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        history_metadata = {}
        if not conversation_id:
            title = await generate_title(request_json["messages"])
            conversation_dict = await current_app.cosmos_conversation_client.create_conversation(
                user_id=user_id, title=title
            )
            conversation_id = conversation_dict["id"]
            history_metadata["title"] = title
            history_metadata["date"] = conversation_dict["createdAt"]

        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "user":
            createdMessageValue = await current_app.cosmos_conversation_client.create_message(
                uuid=str(uuid.uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
            if createdMessageValue == "Conversation not found":
                raise Exception(
                    "Conversation not found for the given conversation ID: "
                    + conversation_id
                    + "."
                )
        else:
            raise Exception("No user message found")

        request_body = await request.get_json()
        history_metadata["conversation_id"] = conversation_id
        request_body["history_metadata"] = history_metadata
        return await conversation_internal(request_body, request.headers)

    except Exception as e:
        logging.exception("Exception in /history/generate")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/update", methods=["POST"])
async def update_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        if not conversation_id:
            raise Exception("No conversation_id found")

        messages = request_json["messages"]
        if len(messages) > 0 and messages[-1]["role"] == "assistant":
            if len(messages) > 1 and messages[-2].get("role", None) == "tool":
                await current_app.cosmos_conversation_client.create_message(
                    uuid=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    user_id=user_id,
                    input_message=messages[-2],
                )
            await current_app.cosmos_conversation_client.create_message(
                uuid=messages[-1]["id"],
                conversation_id=conversation_id,
                user_id=user_id,
                input_message=messages[-1],
            )
        else:
            raise Exception("No bot messages found")

        response = {"success": True}
        return jsonify(response), 200

    except Exception as e:
        logging.exception("Exception in /history/update")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/message_feedback", methods=["POST"])
async def update_message():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    message_id = request_json.get("message_id", None)
    message_feedback = request_json.get("message_feedback", None)
    try:
        if not message_id:
            return jsonify({"error": "message_id is required"}), 400

        if not message_feedback:
            return jsonify({"error": "message_feedback is required"}), 400

        updated_message = await current_app.cosmos_conversation_client.update_message_feedback(
            user_id, message_id, message_feedback
        )
        if updated_message:
            return (
                jsonify(
                    {
                        "message": f"Successfully updated message with feedback {message_feedback}",
                        "message_id": message_id,
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "error": f"Unable to update message {message_id}. It either does not exist or the user does not have access to it."
                    }
                ),
                404,
            )

    except Exception as e:
        logging.exception("Exception in /history/message_feedback")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/delete", methods=["DELETE"])
async def delete_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        deleted_conversation = await current_app.cosmos_conversation_client.delete_conversation(
            user_id, conversation_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted conversation and messages",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/delete")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/list", methods=["GET"])
async def list_conversations():
    await cosmos_db_ready.wait()
    offset = request.args.get("offset", 0)
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    conversations = await current_app.cosmos_conversation_client.get_conversations(
        user_id, offset=offset, limit=25
    )
    if not isinstance(conversations, list):
        return jsonify({"error": f"No conversations for {user_id} were found"}), 404

    return jsonify(conversations), 200

@bp.route("/history/read", methods=["POST"])
async def get_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    conversation_messages = await current_app.cosmos_conversation_client.get_messages(
        user_id, conversation_id
    )

    messages = [
        {
            "id": msg["id"],
            "role": msg["role"],
            "content": msg["content"],
            "createdAt": msg["createdAt"],
            "feedback": msg.get("feedback"),
        }
        for msg in conversation_messages
    ]

    return jsonify({"conversation_id": conversation_id, "messages": messages}), 200

@bp.route("/history/rename", methods=["POST"])
async def rename_conversation():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    if not current_app.cosmos_conversation_client:
        raise Exception("CosmosDB is not configured or not working")

    conversation = await current_app.cosmos_conversation_client.get_conversation(
        user_id, conversation_id
    )
    if not conversation:
        return (
            jsonify(
                {
                    "error": f"Conversation {conversation_id} was not found. It either does not exist or the logged in user does not have access to it."
                }
            ),
            404,
        )

    title = request_json.get("title", None)
    if not title:
        return jsonify({"error": "title is required"}), 400
    conversation["title"] = title
    updated_conversation = await current_app.cosmos_conversation_client.upsert_conversation(
        conversation
    )

    return jsonify(updated_conversation), 200

@bp.route("/history/delete_all", methods=["DELETE"])
async def delete_all_conversations():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    try:
        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        conversations = await current_app.cosmos_conversation_client.get_conversations(
            user_id, offset=0, limit=None
        )
        if not conversations:
            return jsonify({"error": f"No conversations for {user_id} were found"}), 404

        for conversation in conversations:
            await current_app.cosmos_conversation_client.delete_messages(
                conversation["id"], user_id
            )
            await current_app.cosmos_conversation_client.delete_conversation(
                user_id, conversation["id"]
            )
        return (
            jsonify(
                {
                    "message": f"Successfully deleted conversation and messages for user {user_id}"
                }
            ),
            200,
        )

    except Exception as e:
        logging.exception("Exception in /history/delete_all")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/clear", methods=["POST"])
async def clear_messages():
    await cosmos_db_ready.wait()
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]

    request_json = await request.get_json()
    conversation_id = request_json.get("conversation_id", None)

    try:
        if not conversation_id:
            return jsonify({"error": "conversation_id is required"}), 400

        if not current_app.cosmos_conversation_client:
            raise Exception("CosmosDB is not configured or not working")

        deleted_messages = await current_app.cosmos_conversation_client.delete_messages(
            conversation_id, user_id
        )

        return (
            jsonify(
                {
                    "message": "Successfully deleted messages in conversation",
                    "conversation_id": conversation_id,
                }
            ),
            200,
        )
    except Exception as e:
        logging.exception("Exception in /history/clear_messages")
        return jsonify({"error": str(e)}), 500

@bp.route("/history/ensure", methods=["GET"])
async def ensure_cosmos():
    await cosmos_db_ready.wait()
    if not app_settings.chat_history:
        return jsonify({"error": "CosmosDB is not configured"}), 404

    try:
        success, err = await current_app.cosmos_conversation_client.ensure()
        if not current_app.cosmos_conversation_client or not success:
            if err:
                return jsonify({"error": err}), 422
            return jsonify({"error": "CosmosDB is not configured or not working"}), 500

        return jsonify({"message": "CosmosDB is configured and working"}), 200
    except Exception as e:
        logging.exception("Exception in /history/ensure")
        cosmos_exception = str(e)
        if "Invalid credentials" in cosmos_exception:
            return jsonify({"error": cosmos_exception}), 401
        elif "Invalid CosmosDB database name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception} {app_settings.chat_history.database} for account {app_settings.chat_history.account}"
                    }
                ),
                422,
            )
        elif "Invalid CosmosDB container name" in cosmos_exception:
            return (
                jsonify(
                    {
                        "error": f"{cosmos_exception}: {app_settings.chat_history.conversations_container}"
                    }
                ),
                422,
            )
        else:
            return jsonify({"error": "CosmosDB is not working"}), 500

async def generate_title(conversation_messages) -> str:
    title_prompt = "Summarize the conversation so far into a 4-word or less title. Do not use any quotation marks or punctuation. Do not include any other commentary or description."

    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in conversation_messages
    ]
    messages.append({"role": "user", "content": title_prompt})

    try:
        azure_openai_client = await init_openai_client()
        response = await azure_openai_client.chat.completions.create(
            model=app_settings.azure_openai.model, messages=messages, temperature=1, max_tokens=64
        )

        title = response.choices[0].message.content
        return title
    except Exception as e:
        logging.exception("Exception while generating title", e)
        return conversation_messages[-2]["content"]

def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    
    @app.before_serving
    async def init():
        try:
            app.cosmos_conversation_client = await init_cosmosdb_client()
            cosmos_db_ready.set()
        except Exception as e:
            logging.exception("Failed to initialize CosmosDB client")
            app.cosmos_conversation_client = None
            raise e
    
    return app

# Create the application instance
app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
