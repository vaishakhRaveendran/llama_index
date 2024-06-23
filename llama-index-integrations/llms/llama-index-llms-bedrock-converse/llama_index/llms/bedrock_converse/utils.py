import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, MessageRole


logger = logging.getLogger(__name__)

HUMAN_PREFIX = "\n\nHuman:"
ASSISTANT_PREFIX = "\n\nAssistant:"

BEDROCK_MODELS = {
    "amazon.titan-text-express-v1": 8192,
    "amazon.titan-text-lite-v1": 4096,
    "amazon.titan-text-premier-v1:0": 3072,
    "anthropic.claude-instant-v1": 100000,
    "anthropic.claude-v1": 100000,
    "anthropic.claude-v2": 100000,
    "anthropic.claude-v2:1": 200000,
    "anthropic.claude-3-sonnet-20240229-v1:0": 200000,
    "anthropic.claude-3-haiku-20240307-v1:0": 200000,
    "anthropic.claude-3-opus-20240229-v1:0": 200000,
    "anthropic.claude-3-5-sonnet-20240620-v1:0": 200000,
    "ai21.j2-mid-v1": 8192,
    "ai21.j2-ultra-v1": 8192,
    "cohere.command-text-v14": 4096,
    "cohere.command-light-text-v14": 4096,
    "cohere.command-r-v1:0": 128000,
    "cohere.command-r-plus-v1:0": 128000,
    "meta.llama2-13b-chat-v1": 2048,
    "meta.llama2-70b-chat-v1": 4096,
    "meta.llama3-8b-instruct-v1:0": 8192,
    "meta.llama3-70b-instruct-v1:0": 8192,
    "mistral.mistral-7b-instruct-v0:2": 32000,
    "mistral.mixtral-8x7b-instruct-v0:1": 32000,
    "mistral.mistral-large-2402-v1:0": 32000,
    "mistral.mistral-small-2402-v1:0": 32000,
}

BEDROCK_FUNCTION_CALLING_MODELS = (
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "anthropic.claude-3-opus-20240229-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "cohere.command-r-v1:0",
    "cohere.command-r-plus-v1:0",
    "mistral.mistral-large-2402-v1:0",
)


def is_bedrock_function_calling_model(model_name: str) -> bool:
    return model_name in BEDROCK_FUNCTION_CALLING_MODELS


def bedrock_modelname_to_context_size(modelname: str) -> int:
    if modelname not in BEDROCK_MODELS:
        raise ValueError(
            f"Unknown model: {modelname}. Please provide a valid Bedrock model name. "
            "Known models are: " + ", ".join(BEDROCK_MODELS.keys())
        )

    return BEDROCK_MODELS[modelname]


def __merge_common_role_msgs(
    messages: Sequence[Dict[str, Any]],
) -> Sequence[Dict[str, Any]]:
    """Merge consecutive messages with the same role."""
    postprocessed_messages: Sequence[Dict[str, Any]] = []
    for message in messages:
        if (
            postprocessed_messages
            and postprocessed_messages[-1]["role"] == message["role"]
        ):
            postprocessed_messages[-1]["content"] += message["content"]
        else:
            postprocessed_messages.append(message)
    return postprocessed_messages


def messages_to_converse_messages(
    messages: Sequence[ChatMessage],
) -> Tuple[Sequence[Dict[str, Any]], str]:
    """
    Converts a list of generic ChatMessages to AWS Bedrock Converse messages.

    Args:
        messages: List of ChatMessages

    Returns:
        Tuple of:
        - List of AWS Bedrock Converse messages
        - System prompt
    """
    converse_messages = []
    system_prompt = ""
    for message in messages:
        if message.role == MessageRole.SYSTEM:
            # get the system prompt
            system_prompt += message.content + "\n"
        elif message.role == MessageRole.FUNCTION or message.role == MessageRole.TOOL:
            # convert tool output to the AWS Bedrock Converse format
            content = {
                "toolResult": {
                    "toolUseId": message.additional_kwargs["tool_call_id"],
                    "content": [
                        {
                            "text": message.content,
                        },
                    ],
                }
            }
            status = message.additional_kwargs.get("status")
            if status:
                content["toolResult"]["status"] = status
            converse_message = {
                "role": "user",
                "content": [content],
            }
            converse_messages.append(converse_message)
        else:
            content = []
            if message.content:
                # get the text of the message
                content.append({"text": message.content})
            # convert tool calls to the AWS Bedrock Converse format
            tool_calls = message.additional_kwargs.get("tool_calls", [])
            for tool_call in tool_calls:
                assert "toolUseId" in tool_call, f"`toolUseId` not found in {tool_call}"
                assert "input" in tool_call, f"`input` not found in {tool_call}"
                assert "name" in tool_call, f"`name` not found in {tool_call}"
                content.append({"toolUse": tool_call})
            converse_message = {
                "role": message.role.value,
                "content": content,
            }
            converse_messages.append(converse_message)

    return __merge_common_role_msgs(converse_messages), system_prompt.strip()


def tools_to_converse_tools(tools: List["BaseTool"]) -> Dict[str, Any]:
    """
    Converts a list of tools to AWS Bedrock Converse tools.

    Args:
        tools: List of BaseTools

    Returns:
        AWS Bedrock Converse tools
    """
    converse_tools = []
    for tool in tools:
        tool_name, tool_description = getattr(tool, "name", None), getattr(
            tool, "description", None
        )
        if not tool_name or not tool_description:
            # get the tool's name and description from the metadata if they aren't defined
            tool_name = getattr(tool.metadata, "name", None)
            if tool_fn := getattr(tool, "fn", None):
                # get the tool's description from the function's docstring
                tool_description = tool_fn.__doc__
                if not tool_name:
                    tool_name = tool_fn.__name__
            else:
                tool_description = getattr(tool.metadata, "description", None)
            if not tool_name or not tool_description:
                raise ValueError(f"Tool {tool} does not have a name or description.")
        tool_dict = {
            "name": tool_name,
            "description": tool_description,
            # get the schema of the tool's input parameters in the format expected by AWS Bedrock Converse
            "inputSchema": {"json": tool.metadata.get_parameters_dict()},
        }
        converse_tools.append({"toolSpec": tool_dict})
    return {"tools": converse_tools}


def force_single_tool_call(response: ChatResponse) -> None:
    tool_calls = response.message.additional_kwargs.get("tool_calls", [])
    if len(tool_calls) > 1:
        response.message.additional_kwargs["tool_calls"] = [tool_calls[0]]


def _create_retry_decorator(client: Any, max_retries: int) -> Callable[[Any], Any]:
    min_seconds = 4
    max_seconds = 10
    # Wait 2^x * 1 second between each retry starting with
    # 4 seconds, then up to 10 seconds, then 10 seconds afterwards
    try:
        import boto3  # noqa
    except ImportError as e:
        raise ImportError(
            "You must install the `boto3` package to use Bedrock."
            "Please `pip install boto3`"
        ) from e

    return retry(
        reraise=True,
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=min_seconds, max=max_seconds),
        retry=(retry_if_exception_type(client.exceptions.ThrottlingException)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


def converse_with_retry(
    client: Any,
    model: str,
    messages: Sequence[Dict[str, Any]],
    max_retries: int = 3,
    system_prompt: Optional[str] = None,
    max_tokens: int = 1000,
    temperature: float = 0.1,
    stream: bool = False,
    **kwargs: Any,
) -> Any:
    """Use tenacity to retry the completion call."""
    retry_decorator = _create_retry_decorator(client=client, max_retries=max_retries)
    converse_kwargs = {
        "modelId": model,
        "messages": messages,
        "inferenceConfig": {
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    }
    if system_prompt:
        converse_kwargs["system"] = [{"text": system_prompt}]
    if tool_config := kwargs.get("tools"):
        converse_kwargs["toolConfig"] = tool_config
    converse_kwargs = join_two_dicts(
        converse_kwargs, {k: v for k, v in kwargs.items() if k != "tools"}
    )

    @retry_decorator
    def _conversion_with_retry(**kwargs: Any) -> Any:
        if stream:
            return client.converse_stream(**kwargs)
        return client.converse(**kwargs)

    return _conversion_with_retry(**converse_kwargs)


def join_two_dicts(dict1: Dict[str, Any], dict2: Dict[str, Any]) -> Dict[str, Any]:
    """
    Joins two dictionaries, summing shared keys and adding new keys.

    Args:
        dict1: First dictionary
        dict2: Second dictionary

    Returns:
        Joined dictionary
    """
    new_dict = dict1.copy()
    for key, value in dict2.items():
        if key not in new_dict:
            new_dict[key] = value
        if key in new_dict:
            if isinstance(value, dict):
                new_dict[key] = join_two_dicts(new_dict[key], value)
            else:
                new_dict[key] += value
    return new_dict
