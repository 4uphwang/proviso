"""로컬 임베딩 모델(sentence-transformers). 맥에서는 MPS(GPU)를 쓴다.

모델은 처음 쓸 때 Hugging Face에서 받아 ~/.cache/huggingface에 둔다. API 비용은 없다.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=2)
def load_model(name: str):
    import torch
    from sentence_transformers import SentenceTransformer

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    return SentenceTransformer(name, device=device)


class Embedder:
    def __init__(self, model: str, max_length: int = 512, batch_size: int = 16):
        self.model_name = model
        self.max_length = max_length
        self.batch_size = batch_size

    @property
    def _m(self):
        m = load_model(self.model_name)
        m.max_seq_length = self.max_length
        return m

    def tokenize_offsets(self, text: str) -> list[tuple[int, int]]:
        """토큰별 글자 위치(특수 토큰 제외). 고정 길이 청킹이 이 토크나이저로 길이를 센다."""
        enc = self._m.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)
        return [tuple(o) for o in enc["offset_mapping"] if o[1] > o[0]]

    def encode(self, texts: list[str]) -> list[list[float]]:
        vecs = self._m.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                              convert_to_numpy=True, show_progress_bar=False)
        return vecs.tolist()

    def describe(self) -> dict:
        return {"model": self.model_name, "max_length": self.max_length, "normalize": True}
