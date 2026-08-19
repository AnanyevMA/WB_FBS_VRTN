"""
Agent Configuration & PoLP Manifest Module — WB FBS Manager

Provides Pydantic models for parsing agents_config.json, enforcing Principle of
Least Privilege (PoLP) file and resource access control, and inspecting multi-agent specs.
"""
from fnmatch import fnmatch
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "agents_config.json"


class AgentPoLPMatrix(BaseModel):
    model_config = {"extra": "allow"}
    allowed_read_paths: List[str] = Field(default_factory=list)
    allowed_write_paths: List[str] = Field(default_factory=list)
    allowed_delete_paths: List[str] = Field(default_factory=list)
    forbidden_paths: List[str] = Field(default_factory=list)
    database_table_permissions: Dict[str, str] = Field(default_factory=dict)
    external_api_access: List[str] = Field(default_factory=list)
    crypto_key_access: str = "NONE"


class AgentConfig(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    name: str
    role: str
    celery_task: str
    queue: str
    schedule: str
    timeout_seconds: int = 300
    max_retries: int = 3
    concurrency_limit: int = 1
    arbitration_priority: int = 50
    enabled: bool = True
    polp_matrix: AgentPoLPMatrix = Field(default_factory=AgentPoLPMatrix)
    dependencies: List[str] = Field(default_factory=list)


class GlobalPoLPPolicy(BaseModel):
    model_config = {"extra": "allow"}
    default_deny: bool = True
    strict_isolation: bool = True
    audit_logging_enabled: bool = True
    allow_root_execution: bool = False
    global_forbidden_paths: List[str] = Field(default_factory=list)


class ArbitrationRules(BaseModel):
    model_config = {"extra": "allow"}
    priority_queues: Dict[str, int] = Field(default_factory=dict)
    deadlock_prevention: Dict[str, str | int | bool] = Field(default_factory=dict)
    rate_limiting: Dict[str, int] = Field(default_factory=dict)


class AgentsManifest(BaseModel):
    model_config = {"extra": "allow"}
    version: str = "1.0.0"
    system_name: str = "WB FBS Manager Multi-Agent System"
    description: str = ""
    updated_at: Optional[str] = None
    global_polp_policy: GlobalPoLPPolicy = Field(default_factory=GlobalPoLPPolicy)
    celery_queues: List[str] = Field(default_factory=list)
    agents: List[AgentConfig] = Field(default_factory=list)
    arbitration_rules: ArbitrationRules = Field(default_factory=ArbitrationRules)

    def get_agent(self, agent_id: str) -> Optional[AgentConfig]:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        return None

    def list_agent_ids(self) -> List[str]:
        return [a.id for a in self.agents]


def load_manifest(manifest_path: Optional[Path] = None) -> AgentsManifest:
    target_path = manifest_path or DEFAULT_MANIFEST_PATH
    if not target_path.exists():
        # Fallback search in working dir
        root_path = Path.cwd() / "agents_config.json"
        if root_path.exists():
            target_path = root_path
        else:
            raise FileNotFoundError(f"agents_config.json not found at {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    return AgentsManifest(**raw_data)


class PoLPEnforcer:
    """Enforces file path access rights and resource constraints based on PoLP matrix."""

    def __init__(self, manifest: Optional[AgentsManifest] = None):
        self.manifest = manifest or load_manifest()

    def _normalize_path(self, target_path: str) -> str:
        return target_path.replace("\\", "/").strip("/")

    def is_forbidden_globally(self, target_path: str) -> bool:
        norm_path = self._normalize_path(target_path)
        for pattern in self.manifest.global_polp_policy.global_forbidden_paths:
            norm_pattern = pattern.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_pattern) or fnmatch(Path(norm_path).name, norm_pattern):
                return True
        return False

    def can_read(self, agent_id: str, target_path: str) -> bool:
        if self.is_forbidden_globally(target_path):
            return False

        agent = self.manifest.get_agent(agent_id)
        if not agent or not agent.enabled:
            return False

        norm_path = self._normalize_path(target_path)

        # Check agent forbidden paths
        for forb in agent.polp_matrix.forbidden_paths:
            norm_forb = forb.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_forb) or fnmatch(Path(norm_path).name, norm_forb):
                return False

        # Check allowed read paths
        for allow in agent.polp_matrix.allowed_read_paths:
            norm_allow = allow.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_allow) or norm_path.startswith(norm_allow.rstrip("*")):
                return True

        return not self.manifest.global_polp_policy.default_deny

    def can_write(self, agent_id: str, target_path: str) -> bool:
        if self.is_forbidden_globally(target_path):
            return False

        agent = self.manifest.get_agent(agent_id)
        if not agent or not agent.enabled:
            return False

        norm_path = self._normalize_path(target_path)

        for forb in agent.polp_matrix.forbidden_paths:
            norm_forb = forb.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_forb) or fnmatch(Path(norm_path).name, norm_forb):
                return False

        for allow in agent.polp_matrix.allowed_write_paths:
            norm_allow = allow.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_allow) or norm_path.startswith(norm_allow.rstrip("*")):
                return True

        return False

    def can_delete(self, agent_id: str, target_path: str) -> bool:
        if self.is_forbidden_globally(target_path):
            return False

        agent = self.manifest.get_agent(agent_id)
        if not agent or not agent.enabled:
            return False

        norm_path = self._normalize_path(target_path)

        for forb in agent.polp_matrix.forbidden_paths:
            norm_forb = forb.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_forb) or fnmatch(Path(norm_path).name, norm_forb):
                return False

        for allow in agent.polp_matrix.allowed_delete_paths:
            norm_allow = allow.replace("\\", "/").strip("/")
            if fnmatch(norm_path, norm_allow) or norm_path.startswith(norm_allow.rstrip("*")):
                return True

        return False

    def get_table_permission(self, agent_id: str, table_name: str) -> str:
        agent = self.manifest.get_agent(agent_id)
        if not agent:
            return "NONE"
        return agent.polp_matrix.database_table_permissions.get(table_name, "NONE")
