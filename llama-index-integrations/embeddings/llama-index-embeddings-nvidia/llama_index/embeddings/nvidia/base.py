"""NVIDIA embeddings file."""

from typing import Any, List, Literal, Optional
import warnings
from deprecated import deprecated

from llama_index.core.base.embeddings.base import (
    DEFAULT_EMBED_BATCH_SIZE,
    BaseEmbedding,
)
from llama_index.core.bridge.pydantic import Field, PrivateAttr, BaseModel
from llama_index.core.callbacks.base import CallbackManager
from llama_index.core.base.llms.generic_utils import get_from_param_or_env

from openai import OpenAI, AsyncOpenAI

# integrate.api.nvidia.com is the default url for most models, any
# bespoke endpoints will need to be added to the MODEL_ENDPOINT_MAP
BASE_URL = "https://integrate.api.nvidia.com/v1/"
DEFAULT_MODEL = "NV-Embed-QA"

# because MODEL_ENDPOINT_MAP is used to construct KNOWN_URLS, we need to
# include at least one model w/ https://integrate.api.nvidia.com/v1/
MODEL_ENDPOINT_MAP = {
    "NV-Embed-QA": "https://ai.api.nvidia.com/v1/retrieval/nvidia/",
    "snowflake/arctic-embed-l": "https://ai.api.nvidia.com/v1/retrieval/snowflake/arctic-embed-l",
    "nvidia/nv-embed-v1": "https://integrate.api.nvidia.com/v1/",
}

KNOWN_URLS = list(MODEL_ENDPOINT_MAP.values())


class Model(BaseModel):
    id: str


class NVIDIAEmbedding(BaseEmbedding):
    """NVIDIA embeddings."""

    model: str = Field(
        default=DEFAULT_MODEL,
        description="Name of the NVIDIA embedding model to use.\n"
        "Defaults to 'NV-Embed-QA'.",
    )

    truncate: Literal["NONE", "START", "END"] = Field(
        default="NONE",
        description=(
            "Truncate input text if it exceeds the model's maximum token length. "
            "Default is 'NONE', which raises an error if an input is too long."
        ),
    )

    timeout: float = Field(
        default=120, description="The timeout for the API request in seconds.", gte=0
    )

    max_retries: int = Field(
        default=5,
        description="The maximum number of retries for the API request.",
        gte=0,
    )

    _client: Any = PrivateAttr()
    _aclient: Any = PrivateAttr()
    _mode: str = PrivateAttr("nvidia")
    _is_hosted: bool = PrivateAttr(True)

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: Optional[float] = 120,
        max_retries: Optional[int] = 5,
        nvidia_api_key: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        embed_batch_size: int = DEFAULT_EMBED_BATCH_SIZE,  # This could default to 50
        callback_manager: Optional[CallbackManager] = None,
        **kwargs: Any,
    ):
        """
        Construct an Embedding interface for NVIDIA NIM.

        This constructor initializes an instance of the NVIDIAEmbedding class, which provides
        an interface for embedding text using NVIDIA's NIM service.

        Parameters:
        - model (str, optional): The name of the model to use for embeddings.
        - timeout (float, optional): The timeout for requests to the NIM service, in seconds. Defaults to 120.
        - max_retries (int, optional): The maximum number of retries for requests to the NIM service. Defaults to 5.
        - nvidia_api_key (str, optional): The API key for the NIM service. This is required if using a hosted NIM.
        - api_key (str, optional): An alternative parameter for providing the API key.
        - base_url (str, optional): The base URL for the NIM service. If not provided, the service will default to a hosted NIM.
        - **kwargs: Additional keyword arguments.

        API Keys:
        - The recommended way to provide the API key is through the `NVIDIA_API_KEY` environment variable.

        Note:
        - Switch from a hosted NIM (default) to an on-premises NIM using the `base_url` parameter. An API key is required for hosted NIM.
        """
        super().__init__(
            model=model,
            embed_batch_size=embed_batch_size,
            callback_manager=callback_manager,
            **kwargs,
        )

        if embed_batch_size > 259:
            raise ValueError("The batch size should not be larger than 259.")

        if base_url is None:
            # TODO: we should not assume unknown models are at the base url, but
            #       we cannot error out here because
            #          NVIDIAEmbedding(model="special").mode("nim", base_url=...)
            #       is valid usage
            base_url = MODEL_ENDPOINT_MAP.get(model, BASE_URL)

        api_key = get_from_param_or_env(
            "api_key",
            nvidia_api_key or api_key,
            "NVIDIA_API_KEY",
            "NO_API_KEY_PROVIDED",
        )

        self._is_hosted = base_url in KNOWN_URLS

        if self._is_hosted and api_key == "NO_API_KEY_PROVIDED":
            warnings.warn(
                "An API key is required for hosted NIM. This will become an error in 0.2.0."
            )

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._client._custom_headers = {"User-Agent": "llama-index-embeddings-nvidia"}

        self._aclient = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self._aclient._custom_headers = {"User-Agent": "llama-index-embeddings-nvidia"}

    @property
    def available_models(self) -> List[Model]:
        """Get available models."""
        ids = MODEL_ENDPOINT_MAP.keys()
        # TODO: hosted now has a model listing, need to merge known and listed models
        if not self._is_hosted:
            ids = [model.id for model in self._client.models.list()]
        return [Model(id=id) for id in ids]

    @classmethod
    def class_name(cls) -> str:
        return "NVIDIAEmbedding"

    @deprecated(
        version="0.1.2",
        reason="Will be removed in 0.2. Construct with `base_url` instead.",
    )
    def mode(
        self,
        mode: Optional[Literal["nvidia", "nim"]] = "nvidia",
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> "NVIDIAEmbedding":
        """
        Deprecated: use NVIDIAEmbedding(base_url="...") instead.
        """
        if mode == "nim":
            if not base_url:
                raise ValueError("base_url is required for nim mode")
        if mode == "nvidia":
            api_key = get_from_param_or_env("api_key", api_key, "NVIDIA_API_KEY")
        if not base_url:
            # TODO: we should not assume unknown models are at the base url
            base_url = MODEL_ENDPOINT_MAP.get(model or self.model, BASE_URL)

        self._mode = mode
        self._is_hosted = base_url in KNOWN_URLS
        if base_url:
            self._client.base_url = base_url
            self._aclient.base_url = base_url
        if model:
            self.model = model
            self._client.model = model
            self._aclient.model = model
        if api_key:
            self._client.api_key = api_key
            self._aclient.api_key = api_key

        return self

    def _get_query_embedding(self, query: str) -> List[float]:
        """Get query embedding."""
        return (
            self._client.embeddings.create(
                input=[query],
                model=self.model,
                extra_body={"input_type": "query", "truncate": self.truncate},
            )
            .data[0]
            .embedding
        )

    def _get_text_embedding(self, text: str) -> List[float]:
        """Get text embedding."""
        return (
            self._client.embeddings.create(
                input=[text],
                model=self.model,
                extra_body={"input_type": "passage", "truncate": self.truncate},
            )
            .data[0]
            .embedding
        )

    def _get_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get text embeddings."""
        assert len(texts) <= 259, "The batch size should not be larger than 259."

        data = self._client.embeddings.create(
            input=texts,
            model=self.model,
            extra_body={"input_type": "passage", "truncate": self.truncate},
        ).data
        return [d.embedding for d in data]

    async def _aget_query_embedding(self, query: str) -> List[float]:
        """Asynchronously get query embedding."""
        return (
            (
                await self._aclient.embeddings.create(
                    input=[query],
                    model=self.model,
                    extra_body={"input_type": "query", "truncate": self.truncate},
                )
            )
            .data[0]
            .embedding
        )

    async def _aget_text_embedding(self, text: str) -> List[float]:
        """Asynchronously get text embedding."""
        return (
            (
                await self._aclient.embeddings.create(
                    input=[text],
                    model=self.model,
                    extra_body={"input_type": "passage", "truncate": self.truncate},
                )
            )
            .data[0]
            .embedding
        )

    async def _aget_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Asynchronously get text embeddings."""
        assert len(texts) <= 259, "The batch size should not be larger than 259."

        data = (
            await self._aclient.embeddings.create(
                input=texts,
                model=self.model,
                extra_body={"input_type": "passage", "truncate": self.truncate},
            )
        ).data
        return [d.embedding for d in data]
