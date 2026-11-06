"""Documentation safety policy in one pass with file-specific diagnostics."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GUIDES = ("README.md", "docs/usage.md", "docs/troubleshooting.md", "docs/architecture.md")
FORBIDDEN = (
    "a sync ends at or under both caps",
    "ends at or under the configured cap",
    "will always stay under",
    "guaranteed to stay below",
    "`.part` files you can delete safely",
)
REQUIRED = {
    "docs/release-verification.md": (
        "hosted GitHub Actions is **non-blocking**",
        "Python 3.13 offline tests",
        "`--no-rotate` is the default",
        "failed unlink",
        "not atomic",
        "orphan `*.part`",
        "read-only",
        "separate destructive approval",
        "exclusive cooperating writers",
    ),
    "docs/usage.md": (
        "can leave the",
        "over cap",
        "refuses a pass that would delete the entire archive",
        "not atomic",
        "exclusive, cooperating writers",
    ),
    "docs/troubleshooting.md": (
        "automatic age-based scavenging is intentionally disabled",
        "Do not assume every `.part` is disposable",
        "inventory exact paths without removing",
    ),
}


def test_documentation_safety_policy():
    errors = []
    for name in dict.fromkeys((*PUBLIC_GUIDES, *REQUIRED)):
        text = (ROOT / name).read_text(encoding="utf-8")
        if name in PUBLIC_GUIDES:
            errors.extend(
                f"{name}: unsafe claim {phrase!r}" for phrase in FORBIDDEN if phrase in text
            )
        errors.extend(
            f"{name}: missing safety statement {phrase!r}"
            for phrase in REQUIRED.get(name, ())
            if phrase not in text
        )
    assert not errors, "\n".join(errors)
