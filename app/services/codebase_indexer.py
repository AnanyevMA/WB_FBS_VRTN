"""
Codebase Semantic & Symbol Indexer — WB FBS Manager

Parses the entire project AST (Python code, FastAPI routes, Celery tasks,
SQLAlchemy models, Pydantic schemas, and Frontend components) to produce:
1. codebase_index.json — Machine-readable symbol & dependency map for token-efficient agent lookup.
2. CODEBASE_MAP.md — Human & AI two-tier architectural reference.

Allows AI agents to discover exact modules, classes, and function signatures in <100 tokens
without reading full multi-kilobyte source files into context.
"""
import ast
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CODEBASE_INDEX_JSON = PROJECT_ROOT / "codebase_index.json"
CODEBASE_MAP_MD = PROJECT_ROOT / "CODEBASE_MAP.md"


class CodebaseIndexer:
    """
    Automated AST-based code scanner, indexer, and query engine.
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or PROJECT_ROOT
        self.index_json_path = self.root_dir / "codebase_index.json"
        self.map_md_path = self.root_dir / "CODEBASE_MAP.md"
        self._index_cache: Optional[Dict[str, Any]] = None

    def _determine_layer(self, rel_path: str) -> str:
        """Classify file into architectural layer."""
        p = rel_path.replace("\\", "/")
        if p.startswith("app/models/"):
            return "models"
        elif p.startswith("app/schemas/"):
            return "schemas"
        elif p.startswith("app/api/"):
            return "api"
        elif p.startswith("app/agents/"):
            return "agents"
        elif p.startswith("app/services/"):
            return "services"
        elif p.startswith("app/"):
            return "core"
        elif p.startswith("frontend/"):
            return "frontend"
        elif p.startswith("tests/"):
            return "tests"
        elif p.startswith("docs/"):
            return "docs"
        elif p.startswith("alembic/"):
            return "migrations"
        return "config"

    def _parse_python_file(self, file_path: Path) -> Dict[str, Any]:
        """Parse a Python source file using AST to extract symbols and contracts."""
        rel_path = file_path.relative_to(self.root_dir).as_posix()
        layer = self._determine_layer(rel_path)
        content = file_path.read_text(encoding="utf-8")
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError as e:
            logger.warning(f"Syntax error parsing {rel_path}: {e}")
            return {
                "file": rel_path,
                "layer": layer,
                "hash": file_hash,
                "error": f"SyntaxError: {e}",
                "classes": [],
                "functions": [],
                "endpoints": [],
                "celery_tasks": [],
                "db_tables": [],
            }

        docstring = ast.get_docstring(tree) or ""
        classes = []
        functions = []
        endpoints = []
        celery_tasks = []
        db_tables = []
        imports = []

        for node in ast.walk(tree):
            # Imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        for node in tree.body:
            # Classes
            if isinstance(node, ast.ClassDef):
                c_doc = ast.get_docstring(node) or ""
                bases = [self._format_node(b) for b in node.bases]
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(item.name)
                    # Check for __tablename__ in models
                    elif isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == "__tablename__":
                                if isinstance(item.value, ast.Constant):
                                    db_tables.append(item.value.value)

                classes.append({
                    "name": node.name,
                    "bases": bases,
                    "docstring": c_doc.split("\n")[0] if c_doc else "",
                    "methods": methods,
                })

            # Top-level Functions & Decorators
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f_doc = ast.get_docstring(node) or ""
                decorators = [self._format_node(d) for d in node.decorator_list]
                is_async = isinstance(node, ast.AsyncFunctionDef)
                args = [a.arg for a in node.args.args]

                # Check for FastAPI route decorators
                for dec_str in decorators:
                    # router.get(...), app.post(...)
                    route_match = re.search(r"(?:router|app)\.(get|post|put|patch|delete)\((?:['\"]([^'\"]+)['\"])?", dec_str)
                    if route_match:
                        http_method = route_match.group(1).upper()
                        route_path = route_match.group(2) or ""
                        endpoints.append({
                            "method": http_method,
                            "path": route_path,
                            "handler": node.name,
                            "docstring": f_doc.split("\n")[0] if f_doc else "",
                        })

                    # Check for Celery task decorators
                    if "celery_app.task" in dec_str or "shared_task" in dec_str:
                        task_name_match = re.search(r"name=['\"]([^'\"]+)['\"]", dec_str)
                        task_name = task_name_match.group(1) if task_name_match else f"{rel_path.replace('/', '.').replace('.py', '')}.{node.name}"
                        celery_tasks.append({
                            "name": task_name,
                            "function": node.name,
                            "docstring": f_doc.split("\n")[0] if f_doc else "",
                        })

                functions.append({
                    "name": node.name,
                    "is_async": is_async,
                    "args": args,
                    "decorators": decorators,
                    "docstring": f_doc.split("\n")[0] if f_doc else "",
                })

        return {
            "file": rel_path,
            "layer": layer,
            "hash": file_hash,
            "docstring": docstring.split("\n")[0] if docstring else "",
            "classes": classes,
            "functions": functions,
            "endpoints": endpoints,
            "celery_tasks": celery_tasks,
            "db_tables": db_tables,
            "imports": sorted(list(set(imports))),
        }

    def _parse_frontend_file(self, file_path: Path) -> Dict[str, Any]:
        """Extract JavaScript functions, modals, and route views from frontend files."""
        rel_path = file_path.relative_to(self.root_dir).as_posix()
        content = file_path.read_text(encoding="utf-8")
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]

        js_functions = re.findall(r"(?:async\s+)?function\s+([a-zA-Z0-9_]+)\s*\(", content)
        data_routes = re.findall(r'data-route=["\']([^"\']+)["\']', content)
        modals = re.findall(r'id=["\']([a-zA-Z0-9_-]*[Mm]odal[a-zA-Z0-9_-]*)["\']', content)

        return {
            "file": rel_path,
            "layer": "frontend",
            "hash": file_hash,
            "docstring": "Single Page Application Dashboard (HTML/CSS/JS)",
            "classes": [],
            "functions": [{"name": f, "is_async": True, "docstring": "Frontend JS Function"} for f in sorted(set(js_functions))],
            "endpoints": [],
            "celery_tasks": [],
            "db_tables": [],
            "routes": sorted(set(data_routes)),
            "modals": sorted(set(modals)),
        }

    def _format_node(self, node: ast.AST) -> str:
        """Format an AST node back into a readable string."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._format_node(node.value)}.{node.attr}"
        elif isinstance(node, ast.Call):
            func_name = self._format_node(node.func)
            args_str = ", ".join(self._format_node(a) for a in node.args)
            return f"{func_name}({args_str})"
        elif isinstance(node, ast.Constant):
            return repr(node.value)
        return ast.dump(node)

    def scan_project(self) -> Dict[str, Any]:
        """Scan entire codebase and build a structured symbol & architecture index."""
        indexed_files = []

        # Scan python files in app, tests, root
        for py_path in sorted(self.root_dir.glob("**/*.py")):
            rel = py_path.relative_to(self.root_dir).as_posix()
            if any(part.startswith(".") or part == "__pycache__" or part == "venv" or part == ".venv" for part in py_path.parts):
                continue
            parsed = self._parse_python_file(py_path)
            indexed_files.append(parsed)

        # Scan frontend files
        for fe_path in sorted((self.root_dir / "frontend").glob("**/*.html")):
            if fe_path.exists():
                parsed_fe = self._parse_frontend_file(fe_path)
                indexed_files.append(parsed_fe)

        index_payload = {
            "version": "1.0.0",
            "system": "WB FBS Manager Codebase Symbol Index",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "description": "Two-tier AST-generated code index. Agents query this index to pinpoint exact functions/classes before reading full files.",
            "total_files": len(indexed_files),
            "files": indexed_files,
        }
        return index_payload

    def save_index(self, index_payload: Optional[Dict[str, Any]] = None) -> None:
        """Save codebase_index.json and generate CODEBASE_MAP.md."""
        payload = index_payload or self.scan_project()

        # Write JSON
        with open(self.index_json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        # Generate CODEBASE_MAP.md
        md_lines = [
            "# 🗺️ Карта Архитектуры и Символов Проекта (Codebase Map)",
            "",
            "> **Автоматически сгенерированный индекс кодовой базы**  ",
            f"> **Дата актуализации**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | **Файлов проиндексировано**: {payload['total_files']}  ",
            "> **Правило для ИИ-Агентов**: Перед открытием файлов используйте этот справочник или `codebase_index.json` для точечной локализации кода и экономии контекстных токенов.",
            "",
            "---",
            "",
            "## 1. Архитектурные Слои и Модули",
            "",
        ]

        # Group by layer
        layers_order = ["models", "schemas", "api", "services", "agents", "core", "frontend", "tests", "config"]
        grouped: Dict[str, List[Dict[str, Any]]] = {layer: [] for layer in layers_order}

        for f_data in payload.get("files", []):
            layer = f_data.get("layer", "config")
            grouped.setdefault(layer, []).append(f_data)

        layer_titles = {
            "models": "🗄️ База Данных & ORM Модели (`app/models/`)",
            "schemas": "📐 Pydantic Схемы & Контракты (`app/schemas/`)",
            "api": "🌐 FastAPI Роутеры & Эндпоинты (`app/api/`)",
            "services": "⚙️ Бизнес-Логика & Клиенты API (`app/services/`)",
            "agents": "🤖 Мультиагентный Слой Celery (`app/agents/`)",
            "core": "🧠 Ядро Системы & Конфигурация (`app/`)",
            "frontend": "🖥️ Пользовательский Интерфейс (`frontend/`)",
            "tests": "🧪 Набор Автотестов (`tests/`)",
            "config": "📄 Системные Конфигурации & Скрипты",
        }

        for layer in layers_order:
            files_in_layer = grouped.get(layer, [])
            if not files_in_layer:
                continue

            md_lines.append(f"### {layer_titles.get(layer, layer.upper())}")
            md_lines.append("")
            md_lines.append("| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |")
            md_lines.append("|---|---|---|---|")

            for item in files_in_layer:
                rel_f = item["file"]
                classes_str = ", ".join(f"`{c['name']}`" for c in item.get("classes", [])) or "—"
                
                # Highlight endpoints or tasks or functions
                if item.get("endpoints"):
                    funcs_str = "<br>".join(f"`{ep['method']} {ep['path']}` → `{ep['handler']}`" for ep in item["endpoints"])
                elif item.get("celery_tasks"):
                    funcs_str = "<br>".join(f"⚙️ `{t['name']}`" for t in item["celery_tasks"])
                else:
                    funcs_list = [f"`{fn['name']}`" for fn in item.get("functions", [])[:4]]
                    if len(item.get("functions", [])) > 4:
                        funcs_list.append(f"+еще {len(item.get('functions', [])) - 4}")
                    funcs_str = ", ".join(funcs_list) or "—"

                desc = item.get("docstring") or "Модуль кодовой базы"
                md_lines.append(f"| [`{rel_f}`](file:///{PROJECT_ROOT.as_posix()}/{rel_f}) | {classes_str} | {funcs_str} | {desc} |")

            md_lines.append("")

        with open(self.map_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

        self._index_cache = payload
        logger.info(f"Codebase index successfully generated: {payload['total_files']} files indexed.")

    def load_index(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load codebase_index.json into memory."""
        if self._index_cache is None or force_reload:
            if not self.index_json_path.exists():
                self.save_index()
            else:
                with open(self.index_json_path, "r", encoding="utf-8") as f:
                    self._index_cache = json.load(f)
        return self._index_cache

    def query(
        self,
        symbol: Optional[str] = None,
        layer: Optional[str] = None,
        file_keyword: Optional[str] = None,
        endpoint_keyword: Optional[str] = None,
        table_name: Optional[str] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Fast token-efficient lookup for AI agents.
        Returns exact file path, class/function signature, docstring without reading full files.
        """
        index_data = self.load_index()
        results = []

        sym_lower = symbol.lower() if symbol else None
        file_kw = file_keyword.lower() if file_keyword else None
        ep_kw = endpoint_keyword.lower() if endpoint_keyword else None

        for item in index_data.get("files", []):
            if layer and item.get("layer") != layer:
                continue
            if file_kw and file_kw not in item.get("file", "").lower():
                continue
            if table_name and table_name.lower() not in [t.lower() for t in item.get("db_tables", [])]:
                continue

            matched_classes = []
            matched_functions = []
            matched_endpoints = []
            matched_tasks = []

            if sym_lower:
                for c in item.get("classes", []):
                    if sym_lower in c["name"].lower() or any(sym_lower in m.lower() for m in c.get("methods", [])):
                        matched_classes.append(c)
                for fn in item.get("functions", []):
                    if sym_lower in fn["name"].lower():
                        matched_functions.append(fn)

            if ep_kw:
                for ep in item.get("endpoints", []):
                    if ep_kw in ep.get("path", "").lower() or ep_kw in ep.get("handler", "").lower():
                        matched_endpoints.append(ep)

            # If specific criteria matched or general file match
            if matched_classes or matched_functions or matched_endpoints or matched_tasks or (not sym_lower and not ep_kw):
                results.append({
                    "file": item["file"],
                    "layer": item["layer"],
                    "docstring": item.get("docstring"),
                    "matched_classes": matched_classes or item.get("classes", [])[:2],
                    "matched_functions": matched_functions or item.get("functions", [])[:3],
                    "matched_endpoints": matched_endpoints or item.get("endpoints", []),
                    "db_tables": item.get("db_tables", []),
                })

        return results[:limit]
