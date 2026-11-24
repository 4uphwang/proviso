"""12장: 질문 지시문은 질문에만 붙고, 설정에 있을 때만 색인 설명에 남는다(옛 색인의 해시가 바뀌지 않게)."""

from proviso.index.embed import Embedder


def test_describe_keeps_old_shape_without_prompt():
    assert Embedder("BAAI/bge-m3").describe() == {"model": "BAAI/bge-m3", "max_length": 512, "normalize": True}
    assert Embedder("q", query_prompt="Q: ").describe()["query_prompt"] == "Q: "


def test_query_prompt_only_on_queries(monkeypatch):
    seen = []
    e = Embedder("q", query_prompt="Q: ")
    monkeypatch.setattr(Embedder, "encode", lambda self, texts, *, prompt=None: seen.append(prompt) or [[0.0]])
    e.encode_query(["질문"])
    e.encode(["조문"])
    assert seen == ["Q: ", None]
