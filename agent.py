import asyncio
import base64
import os
import uuid
from google.genai import types
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.runners import InMemoryRunner, InMemorySessionService, Runner
from google.adk.tools.tool_context import ToolContext
from google.adk.tools.function_tool import FunctionTool
from google.adk.apps import App, ResumabilityConfig

try:
    from dotenv import load_dotenv

    load_dotenv()
    if "GOOGLE_API_KEY" in os.environ:
        print("GOOGLE_API_KEY loaded successfully.")
except Exception as e:
    print(f"Could not load .env file: {e}")

retry_config = types.HttpRetryOptions(
    attempts=5,  
    exp_base=7,  # Delay multiplier
    initial_delay=1,
    http_status_codes=[429, 500, 503, 504],  # Retry on these HTTP errors
)

LARGE_BULK = 4  # 5+ images require approval

def generate_image_bulk(bulk_size: int, tool_context: ToolContext) -> dict:
    """Generates multiple images in bulk using the a model. Require approval if bulk_size is more than LARGE_BULK.

    Args:
        bulk_size (int): The number of images to generate.
    Returns:
        dict: A dictionary containing the status and message or image URLs.
    """
    
    # Add debug logging
    print(f"[DEBUG] generate_image_bulk called with bulk_size={bulk_size}")
    print(f"[DEBUG] tool_confirmation={tool_context.tool_confirmation}")
    
    ## Small bulk, proceed immediately
    if bulk_size <= LARGE_BULK:
        print(f"[DEBUG] Small bulk approved immediately")
        return {
            "status": "success",
            "message": f"Generation for {bulk_size} images approved. Please proceed with generation."
        }
    
    ## This is the first time this tool is called, large bulks need human approval - PAUSE here
    if not tool_context.tool_confirmation:
        print(f"[DEBUG] Requesting confirmation for bulk_size={bulk_size}")
        tool_context.request_confirmation(
            hint=f"Bulk larger than {LARGE_BULK} images require approval. Do you want to proceed with {bulk_size} images?",
            payload={"bulk_size": bulk_size},
        )
        return {
            "status": "pending",
            "message": f"Generation for {bulk_size} images is awaiting approval. Do not generate images until approved.",
        }
    
    # The tool is called again and is now resuming. Handle approval response - RESUME here.
    if tool_context.tool_confirmation.confirmed:
        return {
            "status": "approved",
            "message": f"Generation for {bulk_size} images has been approved. Please proceed with generation."
        }
    else:
        return {
            "status": "rejected",
            "message": f"Bulk for {bulk_size} images was rejected. Do not generate any images."
        }
        
print("✅ Long running function created.")

mcp_image_server = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",  
            args=[
                "-y",  
                "@gongrzhe/image-gen-server",
            ],
            env={"REPLICATE_API_TOKEN": os.getenv("REPLICATE_API_TOKEN"), "MODEL": os.getenv("MODEL")},
        ),
        timeout=30,
    ),
)

print("✅ MCP Tool created.")

# Defining the Agent
image_agent = LlmAgent(
    model=Gemini(model="gemini-2.5-flash-lite", retry_options=retry_config),
    name="image_agent",
    instruction="""You are an image generation coordinator. When a user requests N images:

MANDATORY STEP 1: Extract the number N from the user's request
MANDATORY STEP 2: ALWAYS call generate_image_bulk(bulk_size=N) FIRST - this is REQUIRED
MANDATORY STEP 3: Check the generate_image_bulk response status:
  - "success" or "approved" → Continue to STEP 4
  - "pending" → STOP IMMEDIATELY and wait for approval
  - "rejected" → STOP IMMEDIATELY, do not generate any images
STEP 4: ONLY if approved, generate images in batches (flux-schnell model max = 4 images per call):
  - Calculate: batches needed = ceil(N / 4)
  - Make multiple generate_image calls:
    * First calls: num_outputs=4
    * Last call: num_outputs=(N mod 4) or 4 if evenly divisible
  
Examples:
- 6 images → Call generate_image_bulk(6), then: Call 1: num_outputs=4, Call 2: num_outputs=2
- 8 images → Call generate_image_bulk(8), then: Call 1: num_outputs=4, Call 2: num_outputs=4
- 10 images → Call generate_image_bulk(10), then: Call 1: num_outputs=4, Call 2: num_outputs=4, Call 3: num_outputs=2

CRITICAL RULES:
1. You MUST call generate_image_bulk BEFORE any generate_image calls
2. You MUST NOT call generate_image if generate_image_bulk returns "pending" or "rejected"
3. You MUST make multiple generate_image calls to reach the total requested""",
    tools=[FunctionTool(func=generate_image_bulk), mcp_image_server],
)

# Wrap the agent in a resumable app, a regular LlmAgent is stateless
# The App adds a persistence layer that saves and restores state
image_app = App(
    name="image_coordinator",
    root_agent=image_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)

root_agent = image_app

print("✅ Resumable app created!")

# Create the Runner with in-memory session service
session_service = InMemorySessionService()

# Create the Runner with the resumable app 
image_runner = Runner(
    app=image_app, # Pass the app and not the agent
    session_service=session_service,
)

print("✅ Runner created!")

# Helper function to process events
def check_for_approval(events):
    """Check if any event contain an approval request.
    
    Returns:
        dict with approval details or None
    """
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if (
                    part.function_call
                    and part.function_call.name == "adk_request_confirmation"
                ):
                    return {
                        "approval_id": part.function_call.id,
                        "invocation_id": event.invocation_id  
                    }
    return None

# Helper to print agent response
def print_agent_response(events):
    """Prints the agent's response from the events."""
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"Agent > {part.text}")

# Helper to format the human decision                    
def create_approval_response(approval_info, approved):
    """Create approval response message."""
    confirmation_response = types.FunctionResponse(
        id=approval_info["approval_id"],
        name="adk_request_confirmation",
        response={"confirmed": approved},
    )
    return types.Content(
        role="user", parts=[types.Part(function_response=confirmation_response)]
    )

print("✅ Helper functions defined")

async def run_image_generation_workflow(query: str, auto_approve: bool = True):
    """Runs the image generation workflow with approval handling.
    
    Args:
        query (str): The user query for image generation.
        auto_approve (bool): Whether to automatically approve large bulks.

    """
    print(f"\n{'='*60}")
    print(f"User > {query}")
    
    # Generate unique session ID
    session_id = f"bulk_{uuid.uuid4().hex[:8]}"
    
    # Create session
    await session_service.create_session(
        app_name="image_coordinator", user_id="test_user", session_id=session_id
    )
    
    query_content = types.Content(role="user", parts=[types.Part(text=query)])
    events = []
    
    # Send initial request to the agent, if bulk size is large, the Agent returns the special `adk_request_confirmation` event
    async for event in image_runner.run_async(
        user_id="test_user", session_id=session_id, new_message=query_content
    ):
        events.append(event)
        # Debug: print event details
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"[DEBUG] Text: {part.text}")
                if hasattr(part, 'function_call') and part.function_call:
                    print(f"[DEBUG] Function call: {part.function_call.name}")
                    print(f"[DEBUG] Function call args: {part.function_call.args}") 
                if hasattr(part, 'function_response') and part.function_response:
                    print(f"[DEBUG] Function response: {part.function_response.response}")
    
    print(f"[DEBUG] Total events received: {len(events)}")
    
    # Loop through all the events generated and check if `adk_request_confirmation` is present.
    approval_info = check_for_approval(events)
    
    #  If the event is present, it's a large bulk - HANDLE APPROVAL WORKFLOW
    if approval_info:
        print(f"⏸️  Pausing for approval...")
        
        # Interactive user approval
        if auto_approve:
            print(f"🤔 Human Decision: APPROVE (auto-approved)\n")
            user_approved = True
        else:
            print(f"\n Approval Required:")
            print(f"Bulk size exceeds threshold ({LARGE_BULK} images)")
            user_input = input("Do you want to approve this bulk? (yes/no): ").strip().lower()
            user_approved = user_input in ['yes', 'y', 'approve']
            print(f"✅ Human Decision: {'APPROVE' if user_approved else 'REJECT'}\n")

        async for event in image_runner.run_async(
            user_id="test_user",
            session_id=session_id,
            new_message=create_approval_response(
                approval_info, user_approved
            ),
            invocation_id=approval_info["invocation_id"],
        ): 
            events.append(event)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        print(f"Agent > {part.text}")
                    if hasattr(part, 'function_call') and part.function_call:
                        print(f"[DEBUG] Function call after approval: {part.function_call.name}")
                    if hasattr(part, 'function_response') and part.function_response:
                        print(f"[DEBUG] Function response after approval: {part.function_response.response}")
        
        print(f"[DEBUG] Total events after approval: {len(events)}")
        
        # Check if images were actually generated after approval
        has_images = any(
            hasattr(part, 'function_response') and 
            part.function_response and
            part.function_response.name == 'generate_image'
            for event in events
            if event.content and event.content.parts
            for part in event.content.parts
        )
        
        # If no images generated, prompt agent to continue
        if not has_images and user_approved:
            print("[DEBUG] No images generated yet, prompting agent to continue...")
            continue_message = types.Content(
                role="user", 
                parts=[types.Part(text="Please proceed with generating the images now.")]
            )
            async for event in image_runner.run_async(
                user_id="test_user",
                session_id=session_id,
                new_message=continue_message
            ):
                events.append(event)
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            print(f"Agent > {part.text}")
                        if hasattr(part, 'function_call') and part.function_call:
                            print(f"[DEBUG] Function call: {part.function_call.name}")
                            print(f"[DEBUG] Function call args: {part.function_call.args}")  
                        if hasattr(part, 'function_response') and part.function_response:
                            print(f"[DEBUG] Function response: {part.function_response.response}")
        
    else:
        # If the `adk_request_confirmation` is not present, no approval needed, bulk completed immediately.
        print_agent_response(events)

    print(f"{'='*60}\n")

    # Process images from events after the workflow completes
    import json
    import urllib.request

    image_urls = []
    for event in events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "function_response") and part.function_response:
                    response = part.function_response.response
                    if isinstance(response, dict) and "content" in response:
                        for item in response["content"]:
                            if item.get("type") == "text":
                                try:
                                    # Parse the JSON string containing URLs
                                    urls = json.loads(item["text"])
                                    if isinstance(urls, list):
                                        image_urls.extend(urls)
                                except json.JSONDecodeError:
                                    pass

    # Download and save images
    if image_urls:
        print(f"\n🎨 Generated {len(image_urls)} images:")
        for i, url in enumerate(image_urls, 1):
            try:
                filename = f"generated_image_{i}.png"
                urllib.request.urlretrieve(url, filename)
                print(f"✅ Image {i} saved as '{filename}'")
                print(f"     URL: {url}")
            except Exception as e:
                print(f"❌ Failed to download image {i}: {e}")
    else:
        print("\n❌  No images were generated")

    # Give MCP server time to clean up properly
    await asyncio.sleep(0.5)

print("✅ Workflow function ready")

if __name__ == "__main__":
    import sys
    import io
    
    async def main():
        await run_image_generation_workflow(
            "Generate 1 image of a cat on a space station looking out the window at Earth.", 
            auto_approve=False
        )
    
    # Temporarily redirect stderr to suppress MCP cleanup errors
    original_stderr = sys.stderr
    sys.stderr = io.StringIO()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.stderr = original_stderr
        print("\nProcess interrupted by user")
    except Exception as e:
        sys.stderr = original_stderr
        # Only show errors that aren't MCP cleanup related
        if "cancel scope" not in str(e) and "stdio_client" not in str(e):
            print(f"Error: {e}")
            raise
    finally:
        sys.stderr = original_stderr