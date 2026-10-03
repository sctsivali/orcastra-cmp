"""Redis-style glob matching for the in-process cache."""

import re


def compile_cache_glob(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            parts.append(re.escape(pattern[index]))
        elif char == "[":
            end = index + 1
            while end < len(pattern) and pattern[end] != "]":
                if pattern[end] == "\\" and end + 1 < len(pattern):
                    end += 1
                end += 1
            if end < len(pattern) and end > index + 1:
                body = pattern[index + 1:end]
                negate = body.startswith("^")
                if negate:
                    body = body[1:]
                body = body.replace("[", "\\[")
                if body.startswith("!"):
                    body = "\\!" + body[1:]
                parts.append("[" + ("^" if negate else "") + body + "]")
                index = end
            else:
                parts.append(re.escape(char))
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("".join(parts), re.DOTALL)
