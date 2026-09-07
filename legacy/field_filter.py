"""
field_filter.py
---------------
Admission filter for the citation network.

Two levels:

`is_relevant_paper(fields)` -- the original broad gate on Semantic
Scholar's coarse `fieldsOfStudy` ("Computer Science", "Biology", ...).
Kept for callers that only have field tags to work with.

`is_core_ai_paper(title, venue, abstract, field_of_study)` -- the strict
gate. A paper is admitted only when it is CORE AI / data-science research.
Papers that merely APPLY AI as a tool inside another domain (chemistry,
medicine, finance, manufacturing, robotics, cybersecurity, ...) are
rejected even though Semantic Scholar tags them "Computer Science".

The strict gate runs three checks in order:
  1. broad field gate -- fails fast on Biology-only, Medicine-only, etc.
  2. subdomain gate   -- title/venue/abstract must hit a CORE_AI_SUBDOMAINS term
  3. applied gate     -- reject if an APPLIED_DOMAIN_TERMS signal appears

Matching is plain case-insensitive substring containment against the
concatenated title + venue + abstract, so every term below must be
lowercase. Edit the two vocabularies to tune what the network admits.
"""

from __future__ import annotations

ALLOWED_FIELDS: frozenset[str] = frozenset({
    "Computer Science",
    "Mathematics",
})

EXCLUDED_FIELDS: frozenset[str] = frozenset({
    "Biology",
    "Medicine",
    "Chemistry",
    "Physics",
    "Economics",
    "Finance",
    "Business",
    "Engineering",
    "Geology",
    "Geography",
    "Political Science",
    "Psychology",
    "Sociology",
    "History",
    "Art",
    "Philosophy",
    "Environmental Science",
    "Agricultural And Food Sciences",
    "Materials Science",
    "Linguistics",
})


# ── core AI / data-science subdomains ────────────────────────────────────────
# At least one of these must appear for a paper to be admitted.

CORE_AI_SUBDOMAINS: frozenset[str] = frozenset({
    # models & architectures
    "neural network", "neural networks", "neural", "deep learning",
    "transformer", "attention mechanism", "self-attention",
    "convolutional", "recurrent network", "diffusion model",
    "mixture-of-experts", "mixture of experts", "state space model",
    # language & LLMs
    "language model", "language models", "llm", "large language model",
    "natural language processing", "nlp", "text generation",
    "machine translation", "question answering", "summarization",
    "tokenization", "prompt", "prompting", "in-context learning",
    "chain-of-thought", "chain of thought", "instruction tuning",
    "fine-tuning", "pretraining", "pre-training",
    # learning paradigms
    "machine learning", "reinforcement learning", "rlhf",
    "supervised learning", "unsupervised learning", "self-supervised",
    "semi-supervised", "transfer learning", "meta-learning",
    "few-shot", "zero-shot", "contrastive learning", "curriculum learning",
    "federated learning", "continual learning", "active learning",
    # agents & reasoning
    "agent", "agents", "agentic", "tool use", "tool learning",
    "planning", "reasoning", "multi-agent", "memory",
    # retrieval / knowledge
    "retrieval", "retrieval-augmented", "rag", "information retrieval",
    "embedding", "embeddings", "vector search", "knowledge graph",
    "semantic search", "dense retrieval",
    # vision & multimodal
    "computer vision", "image classification", "object detection",
    "segmentation", "multimodal", "vision-language",
    "speech recognition", "text-to-speech",
    # data science & methods
    "data mining", "recommender system", "recommendation",
    "clustering", "classification", "regression", "time series",
    "graph neural", "representation learning", "dimensionality reduction",
    "bayesian", "probabilistic model", "variational", "generative model",
    "optimization", "gradient descent", "stochastic gradient",
    "statistical learning", "feature selection",
    # evaluation, efficiency, safety of models themselves
    "benchmark", "evaluation", "dataset", "scaling law", "scaling laws",
    "interpretability", "explainability", "alignment", "hallucination",
    "quantization", "distillation", "pruning", "sparsity",
    "inference efficiency", "parameter-efficient", "lora",
    # named architectures & training machinery -- without these, landmark
    # papers like ResNet ("Deep Residual Learning"), "Layer Normalization"
    # and "Generative Adversarial Networks" fall through the gate.
    "residual learning", "residual network", "resnet",
    "normalization", "batch norm", "layer norm",
    "lstm", "long short-term memory", "gru", "rnn", "cnn",
    "encoder", "decoder", "sequence to sequence", "seq2seq",
    "autoencoder", "auto-encoding", "variational bayes",
    "adversarial", "generative adversarial", "gan",
    "backpropagation", "gradient", "dropout", "regularization",
    "overfitting", "loss function", "objective function",
    "image recognition", "image generation", "perceptron",
    "attention", "self-supervision", "latent space",
    "hyperparameter", "model architecture", "training dynamics",
    "stochastic optimization", "convergence",
})


# ── venues that are themselves a core-AI signal ──────────────────────────────
# A paper at one of these is core AI/DS by publication venue, which rescues
# landmark work whose title uses no recognisable keyword.

CORE_AI_VENUES: frozenset[str] = frozenset({
    "neurips", "neural information processing systems", "nips",
    "icml", "international conference on machine learning",
    "iclr", "international conference on learning representations",
    "aaai", "ijcai", "jmlr", "journal of machine learning research",
    "acl", "annual meeting of the association for computational linguistics",
    "emnlp", "empirical methods in natural language processing",
    "naacl", "coling", "tacl", "conll", "eacl",
    "cvpr", "computer vision and pattern recognition",
    "iccv", "eccv", "international conference on computer vision",
    "sigir", "kdd", "knowledge discovery and data mining",
    "wsdm", "recsys", "the web conference",
    "machine learning", "neural computation", "artificial intelligence",
    "pattern analysis and machine intelligence", "tpami",
    "uai", "aistats", "colt",
})


# ── applied-domain signals ───────────────────────────────────────────────────
# Any of these disqualifies a paper: the work is about another field that
# happens to use AI, not about AI/DS itself.

APPLIED_DOMAIN_TERMS: frozenset[str] = frozenset({
    # chemistry & materials
    "chemistry", "chemical", "molecule", "molecular", "drug discovery",
    "drug design", "protein", "compound screening", "catalysis",
    "materials discovery", "crystal structure", "biochemistry",
    # medicine & biology
    "medicine", "medical", "medicinal", "clinical", "clinician",
    "diagnosis", "diagnostic", "patient", "patients", "hospital",
    "disease", "cancer", "oncology", "tumor", "radiology", "radiograph",
    "healthcare", "health care", "biomedical", "genomics", "genome",
    "electronic health record", "drug", "therapy", "therapeutic",
    "epidemiolog", "pathology", "surgery", "surgical", "psychiatric",
    # finance & economics
    "finance", "financial", "trading", "stock market", "portfolio",
    "credit risk", "fraud detection", "banking", "insurance",
    "investment", "asset pricing", "cryptocurrency", "accounting",
    "economic forecasting", "algorithmic trading",
    # manufacturing & industry
    "manufacturing", "industrial", "supply chain", "predictive maintenance",
    "fault diagnosis", "production line", "assembly line", "logistics",
    "quality inspection", "smart factory",
    # robotics & control
    "robot", "robotic", "robotics", "manipulation", "grasping",
    "autonomous driving", "self-driving", "drone", "uav",
    "motion planning", "locomotion", "actuator", "teleoperation",
    # cybersecurity & networking
    "cybersecurity", "cyber security", "intrusion detection", "malware",
    "phishing", "network security", "vulnerability detection",
    "penetration testing", "ransomware", "botnet", "spam detection",
    # other applied domains
    "agriculture", "agricultural", "crop", "climate", "weather forecasting",
    "remote sensing", "satellite imagery", "geospatial",
    "legal", "law", "court", "judicial",
    "education", "student performance", "e-learning",
    "energy grid", "power system", "smart grid",
    "traffic", "transportation", "urban planning",
    "astronomy", "astrophysic", "particle physics",
    "sports analytics", "marketing", "advertising", "e-commerce",
})


def is_relevant_paper(field_of_study: list[str] | None) -> bool:
    """
    Broad gate on Semantic Scholar's coarse fieldsOfStudy.

    Keep when:
      - field_of_study is None or empty (unknown -- give benefit of the doubt)
      - at least one ALLOWED field is present

    Drop when:
      - only EXCLUDED fields are present (no CS/Math at all)
    """
    if not field_of_study:
        return True
    fields = set(field_of_study)
    if fields & ALLOWED_FIELDS:
        return True
    if fields & EXCLUDED_FIELDS and not (fields & ALLOWED_FIELDS):
        return False
    return True


def _haystack(title: str | None, venue: str | None, abstract: str | None) -> str:
    return " ".join(part for part in (title, venue, abstract) if part).lower()


def matched_core_subdomains(text: str) -> set[str]:
    """Which CORE_AI_SUBDOMAINS terms appear in an already-lowercased text."""
    return {term for term in CORE_AI_SUBDOMAINS if term in text}


def matched_applied_terms(text: str) -> set[str]:
    """Which APPLIED_DOMAIN_TERMS appear in an already-lowercased text."""
    return {term for term in APPLIED_DOMAIN_TERMS if term in text}


def is_core_ai_venue(venue: str | None) -> bool:
    """True when the venue itself marks the work as core AI/DS."""
    if not venue:
        return False
    v = venue.lower()
    return any(name in v for name in CORE_AI_VENUES)


def is_core_ai_paper(
    title: str | None,
    venue: str | None = None,
    abstract: str | None = None,
    field_of_study: list[str] | None = None,
) -> bool:
    """
    Strict gate: admit only core AI / data-science research.

    Returns False when the paper is out-of-domain by field tags, shows no
    core AI subdomain signal at all, or carries an applied-domain signal
    (a paper about chemistry/medicine/finance/... that merely uses AI).
    """
    if not is_relevant_paper(field_of_study):
        return False

    text = _haystack(title, venue, abstract)
    if not text:
        return False

    # A core subdomain term OR publication at a core AI venue satisfies the
    # subdomain gate. The venue route matters for landmark papers whose
    # titles carry no recognisable keyword ("Layer Normalization").
    if not matched_core_subdomains(text) and not is_core_ai_venue(venue):
        return False

    # An applied signal disqualifies the paper even when it is dense with
    # core AI terminology -- "Transformers for cancer treatment planning"
    # is oncology research, not AI research.
    if matched_applied_terms(text):
        return False

    return True
