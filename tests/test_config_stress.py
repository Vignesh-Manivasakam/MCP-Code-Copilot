"""
Adversarial empirical stress tests for M1 configuration & packaging.
Validates dynamic resolution, path setters/getters, caching isolation,
ensure_directories edge cases, security whitelist membership, and platform edge cases.
"""
from pathlib import Path
import pytest
from config import Config, _UNSET


class TestDynamicProjectRoot:
    """Hypothesis 1: Dynamic Config.PROJECT_ROOT updates derived paths synchronously without stale caching."""

    def test_dynamic_project_root_switch(self, tmp_path):
        root1 = tmp_path / "project_alpha"
        root1.mkdir()
        root2 = tmp_path / "project_beta"
        root2.mkdir()

        # Step 1: Assign root1
        Config.PROJECT_ROOT = root1
        assert Config.PROJECT_ROOT == root1.resolve()
        assert Config.COPILOT_DIR == root1.resolve() / ".copilot"
        assert Config.HISTORY_DB_PATH == root1.resolve() / ".copilot" / "history.db"
        assert Config.TRACE_LOG_PATH == root1.resolve() / ".copilot" / "logs" / "trace.jsonl"
        assert Config.INDEX_DB_PATH == root1.resolve() / ".copilot" / "index.db"
        assert Config.CONTEXT_FILE_PATH == root1.resolve() / ".copilot" / "context.md"
        assert Config.LOGS_DIR == root1.resolve() / ".copilot" / "logs"

        # Step 2: Switch to root2 dynamically
        Config.PROJECT_ROOT = root2
        assert Config.PROJECT_ROOT == root2.resolve()
        assert Config.COPILOT_DIR == root2.resolve() / ".copilot"
        assert Config.HISTORY_DB_PATH == root2.resolve() / ".copilot" / "history.db"
        assert Config.TRACE_LOG_PATH == root2.resolve() / ".copilot" / "logs" / "trace.jsonl"
        assert Config.INDEX_DB_PATH == root2.resolve() / ".copilot" / "index.db"
        assert Config.CONTEXT_FILE_PATH == root2.resolve() / ".copilot" / "context.md"
        assert Config.LOGS_DIR == root2.resolve() / ".copilot" / "logs"

        # Step 3: Switch back to root1
        Config.PROJECT_ROOT = root1
        assert Config.PROJECT_ROOT == root1.resolve()
        assert Config.HISTORY_DB_PATH == root1.resolve() / ".copilot" / "history.db"

    def test_project_root_type_coercion_and_resolution(self, tmp_path):
        # String coercion
        Config.PROJECT_ROOT = str(tmp_path)
        assert isinstance(Config.PROJECT_ROOT, Path)
        assert Config.PROJECT_ROOT == tmp_path.resolve()

        # Relative path resolution
        rel_dir = Path("dummy_rel_dir")
        Config.PROJECT_ROOT = rel_dir
        assert Config.PROJECT_ROOT.is_absolute()
        assert Config.PROJECT_ROOT == (Path.cwd() / rel_dir).resolve()

        # None assignment
        Config.PROJECT_ROOT = None
        assert Config.PROJECT_ROOT is None
        assert Config.COPILOT_DIR is None
        assert Config.HISTORY_DB_PATH is None
        assert Config.TRACE_LOG_PATH is None
        assert Config.INDEX_DB_PATH is None
        assert Config.CONTEXT_FILE_PATH is None
        assert Config.LOGS_DIR is None

    def test_path_overrides_and_reset(self, tmp_path):
        Config.PROJECT_ROOT = tmp_path
        custom_db = tmp_path / "custom" / "override.db"
        custom_log = tmp_path / "custom" / "override.jsonl"
        custom_idx = tmp_path / "custom" / "override_idx.db"
        custom_ctx = tmp_path / "custom" / "override_ctx.md"

        Config.HISTORY_DB_PATH = custom_db
        Config.TRACE_LOG_PATH = custom_log
        Config.INDEX_DB_PATH = custom_idx
        Config.CONTEXT_FILE_PATH = custom_ctx

        assert Config.HISTORY_DB_PATH == custom_db.resolve()
        assert Config.TRACE_LOG_PATH == custom_log.resolve()
        assert Config.INDEX_DB_PATH == custom_idx.resolve()
        assert Config.CONTEXT_FILE_PATH == custom_ctx.resolve()

        # Shifting PROJECT_ROOT should NOT clobber explicit overrides
        new_root = tmp_path / "new_project"
        new_root.mkdir()
        Config.PROJECT_ROOT = new_root
        assert Config.HISTORY_DB_PATH == custom_db.resolve()
        assert Config.TRACE_LOG_PATH == custom_log.resolve()

        # Config.reset() should restore dynamic calculation
        Config.reset()
        Config.PROJECT_ROOT = new_root
        assert Config.HISTORY_DB_PATH == new_root.resolve() / ".copilot" / "history.db"
        assert Config.TRACE_LOG_PATH == new_root.resolve() / ".copilot" / "logs" / "trace.jsonl"

    def test_instance_access_delegation(self, tmp_path):
        Config.PROJECT_ROOT = tmp_path
        cfg = Config()
        assert cfg.PROJECT_ROOT == tmp_path.resolve()
        assert cfg.COPILOT_DIR == tmp_path.resolve() / ".copilot"
        assert cfg.HISTORY_DB_PATH == tmp_path.resolve() / ".copilot" / "history.db"
        assert cfg.ALLOWED_COMMANDS == Config.ALLOWED_COMMANDS
        assert cfg.MAX_HISTORY_ENTRIES == 20


class TestEnsureDirectories:
    """Hypothesis 2: ensure_directories() handles nested paths, idempotence, and collisions."""

    def test_ensure_directories_creates_hierarchy(self, tmp_path):
        target = tmp_path / "nested" / "workspace"
        Config.PROJECT_ROOT = target
        Config.ensure_directories()

        copilot_dir = target / ".copilot"
        logs_dir = copilot_dir / "logs"
        assert copilot_dir.exists()
        assert copilot_dir.is_dir()
        assert logs_dir.exists()
        assert logs_dir.is_dir()

    def test_ensure_directories_idempotent(self, tmp_path):
        Config.PROJECT_ROOT = tmp_path
        Config.ensure_directories()
        # Second invocation should not raise
        Config.ensure_directories()
        assert (tmp_path / ".copilot").is_dir()

    def test_ensure_directories_when_root_none(self):
        Config.PROJECT_ROOT = None
        # Should execute safely without throwing AttributeError / TypeError
        Config.ensure_directories()

    def test_ensure_directories_file_collision(self, tmp_path):
        # If .copilot exists as a regular file, ensure_directories will raise FileExistsError
        file_copilot = tmp_path / ".copilot"
        file_copilot.write_text("i am a file, not a directory", encoding="utf-8")
        Config.PROJECT_ROOT = tmp_path
        with pytest.raises(FileExistsError):
            Config.ensure_directories()

    def test_set_project_root_lifecycle(self, tmp_path):
        valid_dir = tmp_path / "valid_dir"
        valid_dir.mkdir()
        Config.set_project_root(valid_dir)
        assert Config.PROJECT_ROOT == valid_dir.resolve()
        assert (valid_dir / ".copilot" / "logs").is_dir()

        # Non-existent directory
        non_existent = tmp_path / "ghost_dir"
        with pytest.raises(ValueError, match="does not exist"):
            Config.set_project_root(non_existent)

        # File instead of directory
        dummy_file = tmp_path / "test.txt"
        dummy_file.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError, match="is not a directory"):
            Config.set_project_root(dummy_file)

    def test_validate_config_lifecycle(self, tmp_path):
        Config.PROJECT_ROOT = None
        with pytest.raises(RuntimeError, match="PROJECT_ROOT must be set"):
            Config.validate_config()

        Config.PROJECT_ROOT = tmp_path / "non_existent"
        with pytest.raises(ValueError, match="does not exist"):
            Config.validate_config()

        dummy_file = tmp_path / "a_file.txt"
        dummy_file.write_text("dummy", encoding="utf-8")
        Config.PROJECT_ROOT = dummy_file
        with pytest.raises(ValueError, match="is not a directory"):
            Config.validate_config()

        Config.PROJECT_ROOT = tmp_path
        # Valid: does not raise
        Config.validate_config()


class TestSecurityBoundariesAndWhitelists:
    """Hypothesis 3 & 4: Whitelists and sensitive boundaries handle edge cases and malicious inputs."""

    def test_allowed_commands_exact_membership(self):
        expected_allowed = {
            "python", "pytest", "npm", "pip", "poetry", "uv",
            "cargo", "go", "dotnet", "ruff", "black", "flake8",
            "git", "node"
        }
        assert Config.ALLOWED_COMMANDS == expected_allowed

    def test_allowed_commands_unwhitelisted_rejection(self):
        dangerous_commands = [
            "curl", "wget", "bash", "sh", "powershell", "pwsh", "cmd",
            "rm", "del", "cat", "nc", "ncat", "netcat", "socat",
            "sudo", "su", "chmod", "chown", "mkfs", "dd", "format"
        ]
        for cmd in dangerous_commands:
            assert cmd not in Config.ALLOWED_COMMANDS, f"Dangerous command '{cmd}' should NOT be in ALLOWED_COMMANDS"

    def test_allowed_commands_edge_cases(self):
        # Case sensitivity: set contains lowercase only
        assert "PYTHON" not in Config.ALLOWED_COMMANDS
        assert "Git" not in Config.ALLOWED_COMMANDS
        assert "Pytest" not in Config.ALLOWED_COMMANDS

        # Windows extensions: raw set contains base binary name without extensions
        assert "python.exe" not in Config.ALLOWED_COMMANDS
        assert "npm.cmd" not in Config.ALLOWED_COMMANDS
        assert "pytest.exe" not in Config.ALLOWED_COMMANDS

        # Path traversal / full paths
        assert "/usr/bin/python" not in Config.ALLOWED_COMMANDS
        assert r"C:\Python310\python.exe" not in Config.ALLOWED_COMMANDS
        assert "../../bin/python" not in Config.ALLOWED_COMMANDS

        # Compound command / arguments injection
        assert "python -c 'import os'" not in Config.ALLOWED_COMMANDS
        assert "git status" not in Config.ALLOWED_COMMANDS
        assert "python && rm -rf /" not in Config.ALLOWED_COMMANDS

    def test_sensitive_files_edge_cases(self):
        expected_sensitive = {
            ".env", ".git/config", "id_rsa", "id_ed25519", ".secrets"
        }
        assert Config.SENSITIVE_FILES == expected_sensitive

        # Note for M2: String matching on Windows requires normalization:
        # e.g., ".git\\config" vs ".git/config" or ".ENV" vs ".env"
        assert ".git/config" in Config.SENSITIVE_FILES
        # Document that raw set does NOT match backslash or uppercase:
        assert r".git\config" not in Config.SENSITIVE_FILES
        assert ".ENV" not in Config.SENSITIVE_FILES
        assert "ID_RSA" not in Config.SENSITIVE_FILES

    def test_exclude_dirs_edge_cases(self):
        expected_exclude = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".pytest_cache", ".mcp_index", ".copilot", "build", "dist",
            ".vscode", ".idea"
        }
        assert Config.EXCLUDE_DIRS == expected_exclude
        assert Config.IGNORED_DIRS is Config.EXCLUDE_DIRS  # Alias check

        # Boundary checks
        assert "node_modules" in Config.EXCLUDE_DIRS
        assert "node_modules/" not in Config.EXCLUDE_DIRS
        assert "NODE_MODULES" not in Config.EXCLUDE_DIRS
        assert ".node_modules" not in Config.EXCLUDE_DIRS

    def test_timeout_and_quota_constants(self):
        assert Config.DEFAULT_TIMEOUT_SECONDS == 30
        assert Config.COMMAND_TIMEOUT_MAX == 300
        assert Config.MAX_OUTPUT_LINES == 40
        assert Config.TOKEN_BUDGET_LIMIT == 1_000_000
        assert Config.TOKEN_WARNING_THRESHOLD == 0.8
        assert Config.CHARS_PER_TOKEN == 4.0
        assert Config.ENABLE_TELEMETRY is True
        assert Config.MAX_HISTORY_ENTRIES == 20
        assert Config.MAX_ENTRIES == 20
        assert Config.CORS_ALLOW_PRIVATE_NETWORK is True
        assert Config.HOST_ORIGIN_PROTECTION is False
