"""Shared Levenshtein distance and detailed edit accounting."""

try:
    from rapidfuzz.distance import Levenshtein
except ImportError:
    Levenshtein = None


def _distance_fallback(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, ref_item in enumerate(reference, 1):
        current = [i]
        for j, hyp_item in enumerate(hypothesis, 1):
            current.append(min(
                current[-1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ref_item != hyp_item),
            ))
        previous = current
    return previous[-1]


def levenshtein_distance(reference, hypothesis):
    if Levenshtein is not None:
        return Levenshtein.distance(reference, hypothesis)
    return _distance_fallback(reference, hypothesis)


def edit_details(reference, hypothesis):
    """Return deterministic insertion/deletion/substitution counts in O(m) memory."""
    previous = [(j, j, 0, 0) for j in range(len(hypothesis) + 1)]
    for i, ref_item in enumerate(reference, 1):
        current = [(i, 0, i, 0)]
        for j, hyp_item in enumerate(hypothesis, 1):
            if ref_item == hyp_item:
                current.append(previous[j - 1])
                continue
            insertion = current[j - 1]
            deletion = previous[j]
            substitution = previous[j - 1]
            candidates = [
                (insertion[0] + 1, insertion[1] + 1,
                 insertion[2], insertion[3]),
                (deletion[0] + 1, deletion[1],
                 deletion[2] + 1, deletion[3]),
                (substitution[0] + 1, substitution[1],
                 substitution[2], substitution[3] + 1),
            ]
            current.append(min(
                candidates,
                key=lambda item: (item[0], item[3], item[2], item[1]),
            ))
        previous = current
    errors, insertions, deletions, substitutions = previous[-1]
    return {
        "errors": errors,
        "reference_words": len(reference),
        "hypothesis_words": len(hypothesis),
        "insertions": insertions,
        "deletions": deletions,
        "substitutions": substitutions,
        "wer": (
            round(errors / len(reference), 4)
            if reference else (0.0 if not hypothesis else None)
        ),
    }
