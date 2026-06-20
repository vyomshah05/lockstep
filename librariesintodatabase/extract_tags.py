"""extract_tags.py — 50 use-case tags per library (no LLM API calls).

Pipeline: build a weighted corpus (purpose-describing parts weighted most) ->
KeyBERT (1-3 grams, MMR for diversity) over a LOCAL all-MiniLM-L6-v2 model ->
normalize (lowercase, lemmatize, drop stopwords + package/ecosystem name tokens)
-> dedupe by stem -> merge near-duplicates by MiniLM cosine -> score, keep top 50
-> backfill from classifiers/keywords if short.

The same MiniLM model produces the catalog embeddings (vector(384)), so the whole
pipeline is API-free. YAKE is the zero-model fallback if KeyBERT/MiniLM are
unavailable.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from functools import lru_cache
from typing import Any

MODEL_NAME = "all-MiniLM-L6-v2"
TARGET_TAGS = 50

# sentence-transformers / torch encode is not guaranteed thread-safe across the
# worker pool, so serialize access to the shared model.
_MODEL_LOCK = threading.Lock()

BUILTIN_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "for", "of",
    "to", "in", "on", "at", "by", "with", "from", "as", "is", "are", "be",
    "this", "that", "these", "those", "it", "its", "you", "your", "we", "our",
    "can", "will", "would", "should", "may", "use", "using", "used", "uses",
    "library", "package", "module", "python", "based", "via", "etc", "e", "g",
    "also", "such", "more", "most", "other", "any", "all", "some", "into",
    "support", "supports", "provide", "provides", "allow", "allows", "simple",
    "easy", "fast", "powerful", "lightweight", "high", "level", "low", "new",
    "https", "http", "www", "com", "org", "io", "github", "docs", "doc",
}


# ---------------------------------------------------------------------------
# Models (lazy, cached)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_model():
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(MODEL_NAME)
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_keybert():
    model = get_model()
    if model is None:
        return None
    try:
        from keybert import KeyBERT
        return KeyBERT(model=model)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _lemmatizer():
    try:
        import nltk
        from nltk.stem import WordNetLemmatizer
        from nltk.corpus import wordnet  # noqa: F401
        try:
            WordNetLemmatizer().lemmatize("tests")
        except LookupError:
            nltk.download("wordnet", quiet=True)
            nltk.download("omw-1.4", quiet=True)
        return WordNetLemmatizer()
    except Exception:
        return None


def embed_text(text: str) -> list[float] | None:
    """Embed a single string to a 384-dim list (or None if model unavailable)."""
    model = get_model()
    if model is None or not text:
        return None
    with _MODEL_LOCK:
        vec = model.encode(text[:4000], normalize_embeddings=True)
    return [float(x) for x in vec]


def embed_texts(texts: list[str]) -> list[list[float] | None]:
    model = get_model()
    if model is None:
        return [None] * len(texts)
    clean = [(t or "")[:4000] for t in texts]
    with _MODEL_LOCK:
        vecs = model.encode(clean, normalize_embeddings=True, batch_size=64)
    return [[float(x) for x in v] for v in vecs]


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------
_PORTER = None


def _stem(word: str) -> str:
    global _PORTER
    if _PORTER is None:
        try:
            from nltk.stem import PorterStemmer
            _PORTER = PorterStemmer()
        except Exception:
            _PORTER = False
    if _PORTER:
        return _PORTER.stem(word)
    # crude fallback: strip common suffixes
    for suf in ("ing", "ers", "er", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _lemma(word: str) -> str:
    lz = _lemmatizer()
    if lz:
        try:
            return lz.lemmatize(word)
        except Exception:
            pass
    return word


def normalize_phrase(phrase: str, banned: set[str]) -> str | None:
    phrase = phrase.lower().strip()
    phrase = re.sub(r"[^a-z0-9\s\-]", " ", phrase)
    tokens = [t for t in re.split(r"[\s\-]+", phrase) if t]
    keep = []
    for t in tokens:
        if t in BUILTIN_STOPWORDS or t in banned:
            continue
        if len(t) < 2 or t.isdigit():
            continue
        keep.append(_lemma(t))
    if not keep:
        return None
    out = " ".join(keep).strip()
    if len(out) < 3:
        return None
    return out


def _stem_key(phrase: str) -> str:
    return " ".join(sorted(_stem(t) for t in phrase.split()))


# ---------------------------------------------------------------------------
# Corpus building
# ---------------------------------------------------------------------------
def build_corpus(summary: str, readme: str, function_summaries: list[str],
                 classifiers: list[str], keywords: list[str]) -> str:
    """Weight purpose-describing text most by repetition."""
    parts: list[str] = []
    purpose = " . ".join(filter(None, [summary] + keywords + classifiers))
    if purpose:
        parts.extend([purpose] * 3)          # weight x3
    if readme:
        parts.append(readme[:4000])
    if function_summaries:
        parts.append(" . ".join(s for s in function_summaries[:200] if s))
    return "\n".join(parts).strip()


def _banned_tokens(name: str, ecosystem: str) -> set[str]:
    banned = {ecosystem.lower(), "npm", "pypi", "cargo", "crate", "golang", "go"}
    for tok in re.split(r"[^a-z0-9]+", name.lower()):
        if tok:
            banned.add(tok)
            banned.add(_lemma(tok))
    return banned


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------
def _keybert_candidates(corpus: str) -> list[tuple[str, float]]:
    kb = get_keybert()
    if kb is None:
        return []
    try:
        with _MODEL_LOCK:
            pairs = kb.extract_keywords(
                corpus,
                keyphrase_ngram_range=(1, 3),
                stop_words="english",
                use_mmr=True,
                diversity=0.6,
                top_n=90,
            )
        return [(p, float(s)) for p, s in pairs]
    except Exception:
        return []


def _yake_candidates(corpus: str) -> list[tuple[str, float]]:
    try:
        import yake
        kw = yake.KeywordExtractor(n=3, top=90, dedupLim=0.7)
        # YAKE: lower score = more relevant -> invert to a 0..1 relevance.
        raw = kw.extract_keywords(corpus)
        if not raw:
            return []
        mx = max(s for _, s in raw) or 1.0
        return [(p, float(1 - s / mx)) for p, s in raw]
    except Exception:
        return []


def _merge_near_duplicates(ranked: list[tuple[str, float]], threshold: float = 0.85
                           ) -> list[tuple[str, float]]:
    model = get_model()
    if model is None or len(ranked) < 2:
        return ranked
    try:
        import numpy as np
        phrases = [p for p, _ in ranked]
        with _MODEL_LOCK:
            embs = model.encode(phrases, normalize_embeddings=True)
        kept: list[int] = []
        kept_embs: list[Any] = []
        for i, e in enumerate(embs):
            dup = False
            for ke in kept_embs:
                if float(np.dot(e, ke)) >= threshold:
                    dup = True
                    break
            if not dup:
                kept.append(i)
                kept_embs.append(e)
        return [ranked[i] for i in kept]
    except Exception:
        return ranked


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def extract_tags(name: str, ecosystem: str, summary: str = "", readme: str = "",
                 function_summaries: list[str] | None = None,
                 classifiers: list[str] | None = None,
                 keywords: list[str] | None = None,
                 n: int = TARGET_TAGS) -> list[tuple[str, float]]:
    function_summaries = function_summaries or []
    classifiers = classifiers or []
    keywords = keywords or []

    corpus = build_corpus(summary, readme, function_summaries, classifiers, keywords)
    banned = _banned_tokens(name, ecosystem)

    candidates = _keybert_candidates(corpus) or _yake_candidates(corpus)

    # Normalize + dedupe by stem, keeping the best score per stem key.
    by_stem: dict[str, tuple[str, float]] = {}
    for phrase, score in candidates:
        norm = normalize_phrase(phrase, banned)
        if not norm:
            continue
        key = _stem_key(norm)
        if key not in by_stem or score > by_stem[key][1]:
            by_stem[key] = (norm, score)

    ranked = sorted(by_stem.values(), key=lambda x: x[1], reverse=True)
    ranked = _merge_near_duplicates(ranked)

    # Backfill from classifiers/keywords if we're short of the target.
    if len(ranked) < n:
        have = {_stem_key(p) for p, _ in ranked}
        backfill_src = keywords + _classifier_phrases(classifiers)
        min_score = (ranked[-1][1] if ranked else 0.2)
        for phrase in backfill_src:
            if len(ranked) >= n:
                break
            norm = normalize_phrase(phrase, banned)
            if not norm:
                continue
            key = _stem_key(norm)
            if key in have:
                continue
            have.add(key)
            ranked.append((norm, round(max(0.05, min_score * 0.5), 4)))

    # Last resort: backfill from the most frequent normalized n-grams in the
    # corpus so every library reaches the target tag count.
    if len(ranked) < n:
        have = {_stem_key(p) for p, _ in ranked}
        for phrase, freq in _frequency_phrases(corpus, banned):
            if len(ranked) >= n:
                break
            key = _stem_key(phrase)
            if key in have:
                continue
            have.add(key)
            ranked.append((phrase, round(min(0.04, 0.001 * freq), 4)))

    return ranked[:n]


def _frequency_phrases(corpus: str, banned: set[str]) -> list[tuple[str, int]]:
    """Most frequent normalized unigrams/bigrams from the corpus."""
    words = [w for w in re.split(r"[^a-z0-9]+", corpus.lower()) if w]
    norm_words = []
    for w in words:
        if w in BUILTIN_STOPWORDS or w in banned or len(w) < 3 or w.isdigit():
            continue
        norm_words.append(_lemma(w))
    counts: Counter[str] = Counter(norm_words)
    for i in range(len(norm_words) - 1):
        bg = f"{norm_words[i]} {norm_words[i + 1]}"
        counts[bg] += 1
    ordered = [(p, c) for p, c in counts.most_common() if len(p) >= 3]
    return ordered


def _classifier_phrases(classifiers: list[str]) -> list[str]:
    """Pull the meaningful leaf of Trove classifiers, e.g.
    'Topic :: Scientific/Engineering :: Image Processing' -> 'image processing'."""
    out = []
    for c in classifiers:
        leaf = c.split("::")[-1].strip()
        leaf = leaf.replace("/", " ").strip()
        if leaf and leaf.lower() not in ("python", "implementation"):
            out.append(leaf)
    return out
