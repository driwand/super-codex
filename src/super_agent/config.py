import json
import os
import re
import shutil
import stat
import tempfile
from copy import deepcopy
from pathlib import Path

AGENTS = ("codex", "claude")
HOME_ENV = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}
SHARED_CODEX_HOME_ENV = "SUPER_CODEX_SHARED_CODEX_HOME"
CODEX_SESSION_DIRECTORIES = ("sessions", "archived_sessions")
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,47}$")
PROVIDER_HOME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$")
CODEX_PROFILE_NAMES = ("main", "2", "3", "4", "5")
STARTUP_MODES = ("select", "main")


class ConfigError(RuntimeError):
    pass


def absolute_path(value):
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def ensure_private_directory(path, parents=True):
    path = Path(path)
    if parents:
        missing = []
        current = path
        while True:
            try:
                current.lstat()
                break
            except FileNotFoundError:
                missing.append(current)
                if current.parent == current:
                    break
                current = current.parent
            except OSError as exc:
                raise ConfigError(f"Cannot inspect private directory {current}: {exc}") from exc
        for directory in reversed(missing):
            ensure_private_directory(directory, parents=False)
    try:
        path.mkdir(parents=False, exist_ok=True, mode=0o700)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise ConfigError(f"Cannot create private directory {path}: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ConfigError(f"Refusing non-directory state path: {path}")
        os.fchmod(descriptor, 0o700)
    except OSError as exc:
        raise ConfigError(f"Cannot secure directory {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    return path


def default_config():
    return {
        "version": 2,
        "startupMode": "select",
        "defaults": {"agent": "codex", "profile": "main"},
        "agentDefaults": {"codex": "main", "claude": "main"},
        "profiles": {
            "codex": {
                "main": {"label": "Codex 1", "isolation": "shared"},
            },
            "claude": {
                "main": {"label": "Claude", "isolation": "shared"},
            },
        },
        "profileOrder": {"codex": ["main"], "claude": ["main"]},
        "workspaces": {},
    }


def resolve_home():
    override = os.environ.get("SUPER_AGENT_HOME")
    if override:
        return absolute_path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return absolute_path(base / "super-codex")


class Store:
    def __init__(self, home=None):
        self.home = absolute_path(home) if home else resolve_home()
        self.config_path = self.home / "config.json"

    def _ensure_home(self):
        ensure_private_directory(self.home)

    def load(self):
        self._ensure_home()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.fspath(self.config_path), flags)
        except FileNotFoundError:
            config = default_config()
            self.save(config)
            return config
        except OSError as exc:
            raise ConfigError(f"Cannot open {self.config_path}: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ConfigError(f"Refusing non-regular config file: {self.config_path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                config = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read {self.config_path}: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self.validate(config)
        return config

    def save(self, config):
        self.validate(config)
        self._ensure_home()
        fd, temporary = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=str(self.home))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            secured = os.open(os.fspath(self.config_path), flags)
            try:
                if not stat.S_ISREG(os.fstat(secured).st_mode):
                    raise ConfigError(f"Refusing non-regular config file: {self.config_path}")
                os.fchmod(secured, 0o600)
            finally:
                os.close(secured)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def validate(self, config):
        if not isinstance(config, dict) or config.get("version") != 2:
            version = config.get("version") if isinstance(config, dict) else None
            raise ConfigError(
                f"Unsupported or invalid config version {version!r} in "
                f"{self.config_path}; remove that file to recreate a schema-version 2 "
                "configuration"
            )
        if config.get("startupMode") not in STARTUP_MODES:
            raise ConfigError("Config startupMode must be 'select' or 'main'")
        profiles = config.get("profiles")
        if not isinstance(profiles, dict):
            raise ConfigError("Config profiles must be an object")
        for agent in AGENTS:
            if not isinstance(profiles.get(agent), dict):
                raise ConfigError(f"Missing profiles for {agent}")
            if not profiles[agent]:
                raise ConfigError(f"At least one {agent} profile is required")
            if agent == "codex" and len(profiles[agent]) > len(CODEX_PROFILE_NAMES):
                raise ConfigError("At most 5 Codex profiles are supported")
            provider_homes = set()
            for name, profile in profiles[agent].items():
                if not NAME_PATTERN.match(name):
                    raise ConfigError(f"Invalid profile name: {name}")
                if agent == "codex" and name not in CODEX_PROFILE_NAMES:
                    raise ConfigError("Codex profiles must be named main, 2, 3, 4, or 5")
                if not isinstance(profile, dict):
                    raise ConfigError(f"Profile must be an object: {agent}/{name}")
                label = profile.get("label")
                if not isinstance(label, str) or not label.strip() or len(label) > 80:
                    raise ConfigError(f"Invalid label for {agent}/{name}")
                if profile.get("isolation") not in ("shared", "isolated"):
                    raise ConfigError(f"Invalid isolation for {agent}/{name}")
                provider_home = profile.get("providerHome")
                if profile["isolation"] == "shared":
                    if provider_home is not None:
                        raise ConfigError(
                            f"Shared profile cannot select a provider home: {agent}/{name}"
                        )
                    continue
                provider_home = provider_home or name
                if (
                    not isinstance(provider_home, str)
                    or not PROVIDER_HOME_PATTERN.match(provider_home)
                ):
                    raise ConfigError(f"Invalid provider home for {agent}/{name}")
                if provider_home in provider_homes:
                    raise ConfigError(f"Provider homes must be unique for {agent}")
                provider_homes.add(provider_home)
        if "main" not in profiles["codex"]:
            raise ConfigError("The codex/main profile is required")
        profile_order = config.get("profileOrder")
        if not isinstance(profile_order, dict):
            raise ConfigError("Config profileOrder must be an object")
        for agent in AGENTS:
            order = profile_order.get(agent)
            if not isinstance(order, list) or any(not isinstance(name, str) for name in order):
                raise ConfigError(f"Config profileOrder.{agent} must be a list")
            if len(order) != len(set(order)) or set(order) != set(profiles[agent]):
                raise ConfigError(f"Config profileOrder.{agent} must list every profile once")
        defaults = config.get("defaults")
        if not isinstance(defaults, dict):
            raise ConfigError("Config defaults must be an object")
        if not isinstance(defaults.get("agent"), str) or not isinstance(
            defaults.get("profile"), str
        ):
            raise ConfigError("Config defaults.agent and defaults.profile must be strings")
        self.require_profile(config, defaults.get("agent"), defaults.get("profile"))
        agent_defaults = config.get("agentDefaults")
        if not isinstance(agent_defaults, dict):
            raise ConfigError("Config agentDefaults must be an object")
        for agent in AGENTS:
            if not isinstance(agent_defaults.get(agent), str):
                raise ConfigError(f"Config agentDefaults.{agent} must be a string")
            self.require_profile(config, agent, agent_defaults.get(agent))
        workspaces = config.get("workspaces", {})
        if not isinstance(workspaces, dict):
            raise ConfigError("Config workspaces must be an object")
        for binding in workspaces.values():
            if not isinstance(binding, dict):
                raise ConfigError("Workspace bindings must be objects")
            self.require_profile(config, binding.get("agent"), binding.get("profile"))

    def require_profile(self, config, agent, profile):
        if agent not in AGENTS:
            raise ConfigError(f"Unsupported agent: {agent}")
        profile = self.normalize_profile(agent, profile)
        if profile not in config.get("profiles", {}).get(agent, {}):
            available = ", ".join(self.ordered_profile_names(config, agent)) or "none"
            raise ConfigError(f"Unknown profile: {agent}/{profile}. Available {agent} profiles: {available}")
        return config["profiles"][agent][profile]

    @staticmethod
    def normalize_profile(agent, profile):
        return "main" if agent == "codex" and profile == "1" else profile

    def ordered_profile_names(self, config, agent):
        if agent not in AGENTS:
            raise ConfigError(f"Unsupported agent: {agent}")
        return list(config["profileOrder"][agent])

    def profile_home(self, agent, profile, config=None, create=False):
        config = config or self.load()
        profile = self.normalize_profile(agent, profile)
        data = self.require_profile(config, agent, profile)
        if data["isolation"] == "shared":
            return None
        provider_home = data.get("providerHome", profile)
        return self._provider_home(agent, provider_home, create)

    def _provider_home(self, agent, provider_home, create=False):
        if agent not in AGENTS or not PROVIDER_HOME_PATTERN.match(provider_home):
            raise ConfigError("Invalid provider home")
        path = self.home / "profiles" / agent / provider_home
        if create:
            self._ensure_home()
            current = self.home
            for component in ("profiles", agent, provider_home):
                current = current / component
                ensure_private_directory(current, parents=False)
        return path

    def replacement_environment(self, agent, profile, config):
        profile = self.normalize_profile(agent, profile)
        data = self.require_profile(config, agent, profile)
        if data["isolation"] != "isolated":
            raise ConfigError(
                f"Safe replacement requires an isolated profile: {agent}/{profile}"
            )
        parent = self._provider_home(agent, profile).parent
        self._ensure_home()
        current = self.home
        for component in ("profiles", agent):
            current = current / component
            ensure_private_directory(current, parents=False)
        candidate = Path(
            tempfile.mkdtemp(prefix=f"{profile}-replacement-", dir=str(parent))
        )
        ensure_private_directory(candidate, parents=False)
        env = os.environ.copy()
        if agent == "codex":
            shared_home = self.shared_codex_home(env)
            self._share_codex_sessions(candidate, shared_home)
            env[SHARED_CODEX_HOME_ENV] = str(shared_home)
        env[HOME_ENV[agent]] = str(candidate)
        return candidate.name, env

    def commit_profile_replacement(
        self, config, agent, profile, provider_home, label=None
    ):
        profile = self.normalize_profile(agent, profile)
        data = self.require_profile(config, agent, profile)
        if data["isolation"] != "isolated":
            raise ConfigError(
                f"Safe replacement requires an isolated profile: {agent}/{profile}"
            )
        candidate = self._provider_home(agent, provider_home)
        try:
            candidate_status = candidate.lstat()
        except OSError as exc:
            raise ConfigError(f"Cannot use replacement provider home: {exc}") from exc
        if (
            stat.S_ISLNK(candidate_status.st_mode)
            or not stat.S_ISDIR(candidate_status.st_mode)
            or candidate_status.st_uid != os.getuid()
        ):
            raise ConfigError("Refusing unsafe replacement provider home")
        updated = deepcopy(config)
        updated_data = updated["profiles"][agent][profile]
        previous_home = data.get("providerHome", profile)
        updated_data["providerHome"] = provider_home
        if label is not None:
            display_label = label.strip()
            if not display_label or len(display_label) > 80:
                raise ConfigError("Profile labels must contain 1-80 characters")
            updated_data["label"] = display_label
        self.save(updated)
        config.clear()
        config.update(updated)
        return previous_home

    def discard_provider_home(self, config, agent, provider_home):
        if agent not in AGENTS or not PROVIDER_HOME_PATTERN.match(provider_home):
            raise ConfigError("Invalid provider home")
        for name, profile in config["profiles"][agent].items():
            if profile["isolation"] == "isolated" and profile.get(
                "providerHome", name
            ) == provider_home:
                raise ConfigError(f"Provider home is still active: {agent}/{name}")
        path = self._provider_home(agent, provider_home)
        try:
            status = path.lstat()
        except FileNotFoundError:
            return False
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.getuid()
        ):
            raise ConfigError(f"Refusing unsafe provider home cleanup: {path}")
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise ConfigError(f"Cannot remove unused provider home {path}: {exc}") from exc
        return True

    def shared_codex_home(self, env=None):
        env = env or os.environ
        configured = env.get(SHARED_CODEX_HOME_ENV) or env.get("CODEX_HOME")
        return absolute_path(configured) if configured else absolute_path(Path.home() / ".codex")

    def _share_codex_sessions(self, isolated_home, shared_home):
        isolated_home = absolute_path(isolated_home)
        shared_home = absolute_path(shared_home)
        if isolated_home == shared_home:
            return
        for name in CODEX_SESSION_DIRECTORIES:
            source = shared_home / name
            try:
                source_status = source.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(source_status.st_mode) or not stat.S_ISDIR(source_status.st_mode):
                raise ConfigError(f"Refusing unsafe shared Codex session path: {source}")
            if source_status.st_uid != os.getuid():
                raise ConfigError(f"Shared Codex session path is not owned by this user: {source}")

            destination = isolated_home / name
            try:
                destination_status = destination.lstat()
            except FileNotFoundError:
                destination_status = None
            if destination_status and stat.S_ISLNK(destination_status.st_mode):
                target = Path(os.readlink(destination))
                if not target.is_absolute():
                    target = destination.parent / target
                if absolute_path(target) == source:
                    continue
                raise ConfigError(f"Refusing unexpected Codex session link: {destination}")
            if destination_status:
                if stat.S_ISDIR(destination_status.st_mode) and not any(destination.iterdir()):
                    os.rmdir(destination)
                else:
                    raise ConfigError(
                        f"Cannot share Codex sessions because {destination} already contains data"
                    )
            try:
                os.symlink(source, destination, target_is_directory=True)
            except OSError as exc:
                raise ConfigError(f"Cannot share Codex sessions at {destination}: {exc}") from exc

    def environment(self, agent, profile, config=None):
        config = config or self.load()
        env = os.environ.copy()
        home = self.profile_home(agent, profile, config, create=True)
        if home:
            if agent == "codex":
                shared_home = self.shared_codex_home(env)
                self._share_codex_sessions(home, shared_home)
                env[SHARED_CODEX_HOME_ENV] = str(shared_home)
            env[HOME_ENV[agent]] = str(home)
        else:
            if agent == "codex":
                env[SHARED_CODEX_HOME_ENV] = str(self.shared_codex_home(env))
            env.pop(HOME_ENV[agent], None)
        return env

    def selection(self, config, workspace, agent=None, profile=None):
        binding, matched = self.workspace_binding(config, workspace)
        base = binding or config["defaults"]
        selected_agent = agent or base["agent"]
        if profile:
            selected_profile = profile
        elif agent and agent != base["agent"]:
            selected_profile = config["agentDefaults"][selected_agent]
        else:
            selected_profile = base["profile"]
        selected_profile = self.normalize_profile(selected_agent, selected_profile)
        self.require_profile(config, selected_agent, selected_profile)
        return selected_agent, selected_profile, matched

    def workspace_binding(self, config, workspace):
        current = Path(workspace).expanduser().resolve()
        bindings = config.get("workspaces", {})
        for candidate in (current,) + tuple(current.parents):
            value = bindings.get(str(candidate))
            if value:
                self.require_profile(config, value.get("agent"), value.get("profile"))
                return deepcopy(value), str(candidate)
        return None, None

    def bind(self, config, workspace, agent, profile, globally=False):
        profile = self.normalize_profile(agent, profile)
        self.require_profile(config, agent, profile)
        if globally:
            config["defaults"] = {"agent": agent, "profile": profile}
            config.setdefault("agentDefaults", {})[agent] = profile
        else:
            path = str(Path(workspace).expanduser().resolve())
            config.setdefault("workspaces", {})[path] = {"agent": agent, "profile": profile}
        self.save(config)

    def unbind(self, config, workspace):
        path = str(Path(workspace).expanduser().resolve())
        removed = config.setdefault("workspaces", {}).pop(path, None)
        if removed:
            self.save(config)
        return removed is not None

    def add_profile(self, config, agent, name, label=None, shared=False):
        if agent not in AGENTS:
            raise ConfigError(f"Unsupported agent: {agent}")
        name = self.normalize_profile(agent, name)
        if not NAME_PATTERN.match(name):
            raise ConfigError("Profile names may contain letters, numbers, dot, underscore, and dash")
        if agent == "codex" and name not in CODEX_PROFILE_NAMES:
            raise ConfigError("Codex profiles must be named main, 2, 3, 4, or 5")
        if agent == "codex" and len(config["profiles"][agent]) >= len(CODEX_PROFILE_NAMES):
            raise ConfigError("At most 5 Codex profiles are supported")
        if name in config["profiles"][agent]:
            raise ConfigError(f"Profile already exists: {agent}/{name}")
        default_label = f"Codex {1 if name == 'main' else name}" if agent == "codex" else name
        display_label = (label or default_label).strip()
        if not display_label or len(display_label) > 80:
            raise ConfigError("Profile labels must contain 1-80 characters")
        config["profiles"][agent][name] = {
            "label": display_label,
            "isolation": "shared" if shared else "isolated",
        }
        config["profileOrder"][agent].append(name)
        if not shared:
            self.profile_home(agent, name, config, create=True)
        self.save(config)

    def set_label(self, config, agent, profile, label):
        profile = self.normalize_profile(agent, profile)
        data = self.require_profile(config, agent, profile)
        display_label = label.strip()
        if not display_label or len(display_label) > 80:
            raise ConfigError("Profile labels must contain 1-80 characters")
        data["label"] = display_label
        self.save(config)

    def set_profile_order(self, config, agent, profiles):
        if agent not in AGENTS:
            raise ConfigError(f"Unsupported agent: {agent}")
        normalized = [self.normalize_profile(agent, name) for name in profiles]
        expected = set(config["profiles"][agent])
        if len(normalized) != len(set(normalized)) or set(normalized) != expected:
            current = " ".join(self.ordered_profile_names(config, agent))
            raise ConfigError(f"Order must list every {agent} profile once. Current: {current}")
        config["profileOrder"][agent] = normalized
        self.save(config)

    def set_startup_mode(self, config, mode):
        if mode not in STARTUP_MODES:
            raise ConfigError("Startup mode must be 'select' or 'main'")
        config["startupMode"] = mode
        self.save(config)
