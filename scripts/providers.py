"""Two LLM providers with the same signature. Dict dispatch, no abstractions."""

import requests

TIMEOUT = 90


def call_gemini(prompt, model, api_key):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        },
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise ValueError(f"gemini {r.status_code}: {r.text[:500]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"unexpected gemini response shape: {exc}") from exc


def call_groq(prompt, model, api_key):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"unexpected groq response shape: {exc}") from exc


def call_gemini_decision(context, deals, model, api_key):
    """Evaluate candidate deals against shared policy using Gemini Decisions API.
    Returns a dict mapping deal index to float probability (0.0 to 1.0).
    """
    questions = {}
    for i, deal in enumerate(deals):
        title = (deal.get("title") or "")[:120].replace('"', "'")
        questions[f"deal_{i}"] = {
            "type": "noul",
            "instructions": (
                f"Should Deal [{i}] ('{title}') be posted to @berliner_deals per the "
                f"editorial policy? Answer yes only if it strictly qualifies."
            ),
        }

    model_path = model if model.startswith("models/") else f"models/{model}"
    payload = {
        "model": model_path,
        "input": {
            "contents": [{"parts": [{"text": context}]}]
        },
        "questions": questions,
    }

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/decisions?key={api_key}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise ValueError(f"gemini-decision {r.status_code}: {r.text[:500]}")
    data = r.json()
    answers = data.get("answers", {})
    scores = {}
    for i in range(len(deals)):
        ans = answers.get(f"deal_{i}", {})
        prob = ans.get("noul")
        if prob is None:
            prob = ans.get("result", {}).get("yes", 0.0)
        try:
            scores[i] = float(prob)
        except (TypeError, ValueError):
            scores[i] = 0.0
    return scores


REGISTRY = {
    "gemini": call_gemini,
    "groq": call_groq,
}
