"""로컬 임베딩 모델(sentence-transformers). 맥에서는 MPS(GPU)를 쓴다.

모델은 처음 쓸 때 Hugging Face에서 받아 ~/.cache/huggingface에 둔다. API 비용은 없다.
12장: query_prompt가 있으면 질문에만 그 문장을 앞에 붙여 임베딩한다(Qwen3-Embedding처럼 질문 지시문을 쓰는 모델).
"""

from __future__ import annotations

from functools import lru_cache


def _device() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


@lru_cache(maxsize=2)
def load_model(name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device=_device())


class Embedder:
    def __init__(self, model: str, max_length: int = 512, batch_size: int = 16, query_prompt: str | None = None):
        self.model_name = model
        self.max_length = max_length
        self.batch_size = batch_size
        self.query_prompt = query_prompt

    @property
    def _m(self):
        m = load_model(self.model_name)
        m.max_seq_length = self.max_length
        return m

    def tokenize_offsets(self, text: str) -> list[tuple[int, int]]:
        """토큰별 글자 위치(특수 토큰 제외). 고정 길이 청킹이 이 토크나이저로 길이를 센다."""
        enc = self._m.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)
        return [tuple(o) for o in enc["offset_mapping"] if o[1] > o[0]]

    def encode(self, texts: list[str], *, prompt: str | None = None) -> list[list[float]]:
        vecs = self._m.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, prompt=prompt,
                              convert_to_numpy=True, show_progress_bar=False)
        return vecs.tolist()

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        return self.encode(texts, prompt=self.query_prompt)

    def describe(self) -> dict:
        d = {"model": self.model_name, "max_length": self.max_length, "normalize": True}
        if self.query_prompt:
            d["query_prompt"] = self.query_prompt
        return d


@lru_cache(maxsize=4)
def _tokenizer(name: str):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(name)


def tokenizer_offsets(name: str):
    """청킹 길이를 다른 모델의 토크나이저로 셀 때(12장: 임베딩만 바꾸고 조각은 같게)."""
    def offsets(text: str) -> list[tuple[int, int]]:
        enc = _tokenizer(name)(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)
        return [tuple(o) for o in enc["offset_mapping"] if o[1] > o[0]]
    return offsets
