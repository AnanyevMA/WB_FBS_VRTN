"""
Knowledge Base Service & Fast Two-Tier Search Engine — WB FBS Manager

Provides:
1. Fast two-tier searching across docs/INDEX.json without full document context loading.
2. Integrity validation for markdown files, internal links, and endpoints.
3. Automated index rebuilding and drift detection between code and documentation.
"""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs"
INDEX_JSON_PATH = DOCS_DIR / "INDEX.json"
INDEX_MD_PATH = DOCS_DIR / "INDEX.md"


class KBService:
    """
    Knowledge Base Management & Query Service.
    """

    def __init__(self, docs_dir: Optional[Path] = None):
        self.docs_dir = docs_dir or DOCS_DIR
        self.index_json_path = self.docs_dir / "INDEX.json"
        self.index_md_path = self.docs_dir / "INDEX.md"
        self._index_cache: Optional[Dict[str, Any]] = None

    def load_index(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load and cache docs/INDEX.json."""
        if self._index_cache is None or force_reload:
            if not self.index_json_path.exists():
                logger.warning(f"Index file {self.index_json_path} does not exist. Generating...")
                self.rebuild_index()
            with open(self.index_json_path, "r", encoding="utf-8") as f:
                self._index_cache = json.load(f)
        return self._index_cache

    def search(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        endpoint: Optional[str] = None,
        error_code: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Fast hierarchical search returning only document summaries and exact file paths.
        Agents use this to locate the target document without loading full texts into context.
        """
        index_data = self.load_index()
        docs = index_data.get("documents", [])
        results = []

        query_terms = [q.lower().strip() for q in (query or "").split() if q.strip()]

        for doc in docs:
            score = 0
            doc_category = doc.get("category", "").lower()
            doc_title = doc.get("title", "").lower()
            doc_summary = doc.get("summary", "").lower()
            doc_tags = [t.lower() for t in doc.get("tags", [])]
            doc_endpoints = [ep.lower() for ep in doc.get("endpoints", [])]
            doc_errors = [err.lower() for err in doc.get("covered_errors", [])]

            # Category filter
            if category and category.lower() != doc_category:
                continue

            # Exact endpoint match
            if endpoint:
                ep_clean = endpoint.lower().strip()
                if any(ep_clean in ep for ep in doc_endpoints):
                    score += 50

            # Error code match
            if error_code:
                err_clean = error_code.lower().strip()
                if any(err_clean in err for err in doc_errors):
                    score += 50

            # Tags match
            if tags:
                matched_tags = set(t.lower() for t in tags).intersection(set(doc_tags))
                score += len(matched_tags) * 15

            # Text query matching
            for term in query_terms:
                if term in doc_title:
                    score += 20
                if any(term in t for t in doc_tags):
                    score += 15
                if term in doc_summary:
                    score += 10
                if any(term in ep for ep in doc_endpoints):
                    score += 15
                if any(term in err for err in doc_errors):
                    score += 15

            if score > 0 or (not query and not tags and not endpoint and not error_code):
                res_item = dict(doc)
                res_item["match_score"] = score
                results.append(res_item)

        # Sort by score descending
        results.sort(key=lambda x: x.get("match_score", 0), reverse=True)
        return results[:limit]

    def get_document_content(self, doc_id_or_path: str) -> Optional[str]:
        """
        Load full content of a specific targeted document.
        """
        target_path: Optional[Path] = None

        if doc_id_or_path.endswith(".md"):
            # Path provided
            candidate = self.docs_dir.parent / doc_id_or_path if doc_id_or_path.startswith("docs/") else self.docs_dir / doc_id_or_path
            if candidate.exists():
                target_path = candidate
        else:
            # Doc ID or stem provided
            index_data = self.load_index()
            for doc in index_data.get("documents", []):
                if doc.get("id") == doc_id_or_path or doc_id_or_path in doc.get("file", ""):
                    target_path = self.docs_dir.parent / doc.get("file")
                    break

        if target_path and target_path.exists():
            with open(target_path, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def validate_integrity(self) -> Dict[str, Any]:
        """
        Verify all documents exist, have valid formatting, and no broken internal links.
        """
        index_data = self.load_index(force_reload=True)
        docs = index_data.get("documents", [])
        issues = []
        checked_count = 0

        for doc in docs:
            rel_file = doc.get("file")
            file_path = self.docs_dir.parent / rel_file
            checked_count += 1

            if not file_path.exists():
                issues.append(f"Missing file: {rel_file} (doc_id: {doc.get('id')})")
                continue

            content = file_path.read_text(encoding="utf-8")
            if len(content.strip()) < 50:
                issues.append(f"File {rel_file} is suspiciously short ({len(content)} chars)")

            # Check markdown links
            links = re.findall(r"\[.*?\]\((file:///[^\)]+|[^\)]+\.md(?:#[^\)]*)?)\)", content)
            for link in links:
                link_clean = link.split("#")[0]
                if link_clean.startswith("file:///"):
                    # Local absolute file link
                    clean_path = link_clean.replace("file:///", "").replace("%20", " ")
                    if not Path(clean_path).exists():
                        issues.append(f"Broken file link in {rel_file}: {link}")
                elif link_clean.endswith(".md") and not link_clean.startswith("http"):
                    # Relative markdown link
                    target_md = (file_path.parent / link_clean).resolve()
                    if not target_md.exists():
                        issues.append(f"Broken relative link in {rel_file}: {link}")

        return {
            "status": "HEALTHY" if not issues else "WARNINGS_FOUND",
            "checked_documents_count": checked_count,
            "issues": issues,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    def rebuild_index(self) -> Dict[str, Any]:
        """
        Rebuilds INDEX.json and INDEX.md based on current files in docs/.
        """
        # Ensure base structure
        categories_map = {
            "wb_api": "Wildberries Marketplace API (FBS v3)",
            "chestny_znak_api": "Честный Знак (ГИС МТ & СУЗ-Облако 3.0.38 / True API)",
            "solutions_and_recipes": "Архитектурные решения и Рецепты",
            "troubleshooting": "Диагностика и Справочник Ошибок",
        }

        # Keep existing index structure if valid or generate fresh
        existing = {}
        if self.index_json_path.exists():
            try:
                with open(self.index_json_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                logger.error(f"Error parsing existing INDEX.json: {e}")

        # Scan docs subfolders
        all_docs = []
        for cat_dir in ("wb_api", "chestny_znak_api", "solutions_and_recipes", "troubleshooting"):
            dir_path = self.docs_dir / cat_dir
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)

            for md_file in sorted(dir_path.glob("*.md")):
                rel_path = f"docs/{cat_dir}/{md_file.name}"
                content = md_file.read_text(encoding="utf-8")
                
                # Extract first heading
                title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else md_file.stem

                # Extract doc id
                id_match = re.search(r"(?:\*\*)?Документ ID(?:\*\*)?:\s*`([^`]+)`", content, re.IGNORECASE)
                existing_item = next((d for d in existing.get("documents", []) if d.get("file") == rel_path), None)
                if id_match:
                    doc_id = id_match.group(1).strip()
                elif existing_item and existing_item.get("id"):
                    doc_id = existing_item["id"]
                else:
                    doc_id = f"{cat_dir}_{md_file.stem}"

                # Extract summary or first paragraph
                summary = ""
                for line in content.splitlines():
                    if line.startswith(">") or line.startswith("#") or not line.strip() or line.startswith("---"):
                        continue
                    summary = line.strip()
                    break

                # Extract endpoints
                endpoints = re.findall(r"(GET|POST|PUT|PATCH|DELETE)\s+(/[a-zA-Z0-9_/{}?&=]+)", content)
                ep_list = [f"{m[0]} {m[1]}" for m in endpoints]

                # Match existing item tags or fallback
                tags = existing_item.get("tags", [cat_dir, md_file.stem]) if existing_item else [cat_dir, md_file.stem]
                covered_errors = existing_item.get("covered_errors", []) if existing_item else []
                doc_summary = existing_item.get("summary", summary) if existing_item else summary

                all_docs.append({
                    "id": doc_id,
                    "category": cat_dir,
                    "title": title,
                    "file": rel_path,
                    "summary": doc_summary,
                    "tags": tags,
                    "endpoints": ep_list or (existing_item.get("endpoints", []) if existing_item else []),
                    "covered_errors": covered_errors,
                })

        # Also preserve existing root specification docs or docs outside subdirectories if they exist
        for ex_doc in existing.get("documents", []):
            ex_file = ex_doc.get("file", "")
            if not any(ex_file.startswith(f"docs/{cd}/") for cd in categories_map.keys()):
                target = self.docs_dir.parent / ex_file
                if target.exists():
                    all_docs.append(ex_doc)

        new_index = {
            "version": "1.0.0",
            "system": "WB FBS Manager Knowledge Base",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "description": "High-level two-tier semantic routing and lookup index. Agents must query this index to locate the exact document before loading full content.",
            "categories": [
                {"id": k, "name": v, "description": v}
                for k, v in categories_map.items()
            ],
            "documents": all_docs,
        }

        with open(self.index_json_path, "w", encoding="utf-8") as f:
            json.dump(new_index, f, ensure_ascii=False, indent=2)

        self._index_cache = new_index
        return new_index
