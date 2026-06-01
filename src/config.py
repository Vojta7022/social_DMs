"""
config.py — Load settings.yaml + .env into typed Config dataclasses.

Usage:
    cfg = load_config(Path("settings.yaml"), Path(".env"))
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv
from src.utils import normalize_facebook_profile_identifier


# ─── Exceptions ──────────────────────────────────────────────────────────────

class ConfigError(Exception):
    pass


# ─── Nested config dataclasses ───────────────────────────────────────────────

@dataclass
class StealthConfig:
    delay_mean_ms: int = 1500
    delay_std_ms: int = 400
    delay_min_ms: int = 600
    delay_max_ms: int = 5000
    scroll_steps: int = 8
    typing_wpm: int = 45
    mouse_curve_steps: int = 12


@dataclass
class MediaConfig:
    enabled: bool = True
    max_file_size_mb: int = 500
    photo_format: str = "jpg"
    video_format: str = "mp4"
    yt_dlp_extra_args: list[str] = field(default_factory=list)
    deduplicate_by_hash: bool = True


@dataclass
class ArchiveConfig:
    run_initial_capture_on_startup: bool = False
    notify_on_initial_capture: bool = False
    max_post_scrolls: int = 10
    max_empty_post_scrolls: int = 3
    max_highlight_items_per_collection: int = 25


@dataclass
class ScheduleConfig:
    posts_interval_minutes: int = 30
    stories_interval_minutes: int = 120
    followers_interval_minutes: int = 180
    dms_interval_minutes: int = 60
    profile_rebuild_interval_hours: int = 6
    daily_digest_time: str = "08:00"


@dataclass
class NotificationsConfig:
    new_post: bool = True
    new_story: bool = True
    follower_gained: bool = True
    follower_lost: bool = True
    following_started: bool = True
    following_stopped: bool = True
    error_alerts: bool = True
    daily_digest: bool = True
    # Resolved from env vars after init
    chat_id: str = ""
    general_thread_id: int | None = None
    instagram_posts_thread_id: int | None = None
    instagram_mentions_thread_id: int | None = None
    instagram_reposts_thread_id: int | None = None
    instagram_stories_thread_id: int | None = None
    instagram_highlights_thread_id: int | None = None
    instagram_followers_thread_id: int | None = None
    tiktok_posts_thread_id: int | None = None
    tiktok_reposts_thread_id: int | None = None
    tiktok_stories_thread_id: int | None = None
    tiktok_followers_thread_id: int | None = None
    facebook_posts_thread_id: int | None = None
    facebook_stories_thread_id: int | None = None
    facebook_friends_thread_id: int | None = None
    followers_thread_id: int | None = None
    errors_thread_id: int | None = None
    digital_twin_thread_id: int | None = None

    @staticmethod
    def _first_configured(*thread_ids: int | None) -> int | None:
        """Return the first non-null Telegram topic ID."""
        for thread_id in thread_ids:
            if thread_id is not None:
                return thread_id
        return None

    def general_thread(self) -> int | None:
        """Return the default topic for general operational messages."""
        return self.general_thread_id

    def error_thread(self) -> int | None:
        """Route failures to the dedicated errors topic when configured."""
        return self._first_configured(self.errors_thread_id, self.general_thread_id)

    def digital_twin_thread(self) -> int | None:
        """Route HITL draft review cards into the dedicated Telegram topic when configured."""
        return self._first_configured(self.digital_twin_thread_id, self.general_thread_id)

    def thread_id_for_post(self, platform: str, content_scope: str | None = None) -> int | None:
        """Return the Telegram topic for a post-like event."""
        platform_key = (platform or "").strip().lower()
        scope_key = (content_scope or "feed").strip().lower()

        if platform_key == "instagram":
            if scope_key == "mentions":
                return self._first_configured(
                    self.instagram_mentions_thread_id,
                    self.instagram_posts_thread_id,
                    self.general_thread_id,
                )
            if scope_key == "reposts":
                return self._first_configured(
                    self.instagram_reposts_thread_id,
                    self.instagram_posts_thread_id,
                    self.general_thread_id,
                )
            return self._first_configured(self.instagram_posts_thread_id, self.general_thread_id)

        if platform_key == "tiktok":
            if scope_key == "reposts":
                return self._first_configured(
                    self.tiktok_reposts_thread_id,
                    self.tiktok_posts_thread_id,
                    self.general_thread_id,
                )
            return self._first_configured(self.tiktok_posts_thread_id, self.general_thread_id)

        if platform_key == "facebook":
            return self._first_configured(self.facebook_posts_thread_id, self.general_thread_id)

        return self.general_thread_id

    def thread_id_for_story(self, platform: str, content_scope: str | None = None) -> int | None:
        """Return the Telegram topic for a story or highlight event."""
        platform_key = (platform or "").strip().lower()
        scope_key = (content_scope or "story").strip().lower()

        if platform_key == "instagram":
            if scope_key == "highlight":
                return self._first_configured(
                    self.instagram_highlights_thread_id,
                    self.instagram_stories_thread_id,
                    self.general_thread_id,
                )
            return self._first_configured(self.instagram_stories_thread_id, self.general_thread_id)

        if platform_key == "tiktok":
            return self._first_configured(self.tiktok_stories_thread_id, self.general_thread_id)

        if platform_key == "facebook":
            return self._first_configured(self.facebook_stories_thread_id, self.general_thread_id)

        return self.general_thread_id

    def thread_id_for_connection(self, platform: str) -> int | None:
        """Return the Telegram topic for follower/following/friend events."""
        platform_key = (platform or "").strip().lower()

        if platform_key == "instagram":
            return self._first_configured(
                self.instagram_followers_thread_id,
                self.followers_thread_id,
                self.general_thread_id,
            )
        if platform_key == "tiktok":
            return self._first_configured(
                self.tiktok_followers_thread_id,
                self.followers_thread_id,
                self.general_thread_id,
            )
        if platform_key == "facebook":
            return self._first_configured(self.facebook_friends_thread_id, self.general_thread_id)

        return self._first_configured(self.followers_thread_id, self.general_thread_id)

    def thread_id_for(self, route: str) -> int | None:
        """Compatibility wrapper for older generic routing labels."""
        route_key = (route or "").strip().lower()
        if route_key in {"general", "digest", "startup"}:
            return self.general_thread()
        if route_key == "errors":
            return self.error_thread()
        if route_key == "feed":
            return self._first_configured(
                self.instagram_posts_thread_id,
                self.tiktok_posts_thread_id,
                self.facebook_posts_thread_id,
                self.general_thread_id,
            )
        if route_key == "stories":
            return self._first_configured(
                self.instagram_stories_thread_id,
                self.instagram_highlights_thread_id,
                self.tiktok_stories_thread_id,
                self.facebook_stories_thread_id,
                self.general_thread_id,
            )
        if route_key == "followers":
            return self._first_configured(
                self.instagram_followers_thread_id,
                self.tiktok_followers_thread_id,
                self.facebook_friends_thread_id,
                self.followers_thread_id,
                self.general_thread_id,
            )
        return self.general_thread_id


@dataclass
class ProxyConfig:
    rotation: str = "round_robin"   # round_robin | random
    pool: list[str] = field(default_factory=list)


@dataclass
class AccountConfig:
    platform: str = ""
    username: str = ""
    proxy: str | None = None        # None = use pool
    monitor_own_profile: bool = False


@dataclass
class TargetConfig:
    platform: str = ""
    username: str = ""
    check_posts: bool = True
    check_stories: bool = True
    check_highlights: bool = False
    check_mentions: bool = False
    using_account: str = ""         # resolved login account username used at runtime
    using_accounts: list[str] = field(default_factory=list)  # allowed account pool for auto-routing


@dataclass
class LoggingConfig:
    file: str = "./data/social_scanner.log"
    rotation: str = "10 MB"
    retention: str = "30 days"


@dataclass
class InitiationRuleConfig:
    platform: str = ""
    username: str = ""
    priority: str = "standard"
    story_keywords: list[str] = field(default_factory=list)
    trigger_categories: list[str] = field(default_factory=lambda: ["story_keyword"])
    cooldown_hours: int | None = None
    max_pending_drafts: int | None = None


@dataclass
class DigitalTwinConfig:
    enabled: bool = False
    reply_window_hours: int = 24
    default_model: str = "gemini-1.5-flash"
    style_example_count: int = 8
    profile_context_count: int = 6
    max_pending_drafts_per_target: int = 1
    initiation_cooldown_hours: int = 72
    story_keyword_rules: list[InitiationRuleConfig] = field(default_factory=list)
    appropriate_initiation_rules: list[InitiationRuleConfig] = field(default_factory=list)


# ─── Root config dataclass ───────────────────────────────────────────────────

@dataclass
class Config:
    # General
    log_level: str = "INFO"
    dry_run: bool = False
    # Nested sections
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    stealth: StealthConfig = field(default_factory=StealthConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    digital_twin: DigitalTwinConfig = field(default_factory=DigitalTwinConfig)
    proxies: ProxyConfig = field(default_factory=ProxyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    accounts: list[AccountConfig] = field(default_factory=list)
    targets: list[TargetConfig] = field(default_factory=list)
    # From .env
    telegram_bot_token: str = ""
    gemini_api_key: str = ""
    meta_graph_access_token: str = ""
    meta_graph_api_version: str = "v22.0"
    meta_graph_base_url: str = "https://graph.instagram.com"
    meta_graph_app_secret: str = ""
    meta_graph_ig_user_id: str = ""
    # Derived credential map: (platform, username) → password
    credentials: dict[tuple[str, str], str] = field(default_factory=dict)
    # Derived paths (populated by load_config)
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    db_path: Path = field(default_factory=lambda: Path("./data/social_scanner.db"))
    sessions_dir: Path = field(default_factory=lambda: Path("./data/sessions"))
    media_dir: Path = field(default_factory=lambda: Path("./data/media"))
    profiles_dir: Path = field(default_factory=lambda: Path("./data/profiles"))
    accounts_dir: Path = field(default_factory=lambda: Path("./data/accounts"))
    browser_profile_suffix: str = ""


# ─── Internal helpers ────────────────────────────────────────────────────────

def _credential_env_key(platform: str, username: str) -> str:
    """Build env var name: instagram + my.ig-account → INSTAGRAM_PASSWORD_MY_IG_ACCOUNT."""
    safe_username = username.upper().replace(".", "_").replace("-", "_")
    return f"{platform.upper()}_PASSWORD_{safe_username}"


def _clean_env_value(value: str | None) -> str:
    """
    Normalise env values loaded from dotenv.
    Treat blank strings and comment-like placeholders as unset so optional fields
    can safely fall back to defaults.
    """
    if value is None:
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned.startswith("#"):
        return ""
    return cleaned


def _clean_optional_int(value: str | None, env_name: str) -> int | None:
    """Parse an optional integer env var used for Telegram topic IDs."""
    cleaned = _clean_env_value(value)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {env_name} must be an integer.") from exc


def _load_optional_int_env(*env_names: str) -> int | None:
    """Return the first configured optional integer env var from a list of names."""
    for env_name in env_names:
        value = os.environ.get(env_name)
        if _clean_env_value(value):
            return _clean_optional_int(value, env_name)
    return None


def _parse_accounts(raw: list[dict]) -> list[AccountConfig]:
    result = []
    for item in raw:
        result.append(AccountConfig(
            platform=item["platform"],
            username=item["username"],
            proxy=item.get("proxy") or None,
            monitor_own_profile=item.get("monitor_own_profile", False),
        ))
    return result


def _normalise_target_account_pool(raw_pool: object) -> list[str]:
    """Parse one target's `using_accounts` value into a clean, deduplicated list."""
    if raw_pool is None:
        return []
    if isinstance(raw_pool, str):
        raw_items = [raw_pool]
    elif isinstance(raw_pool, list):
        raw_items = raw_pool
    else:
        raise ConfigError(
            "Invalid target using_accounts value: expected a username string or a list of usernames."
        )

    normalised: list[str] = []
    for item in raw_items:
        username = str(item or "").strip()
        if username and username not in normalised:
            normalised.append(username)
    return normalised


def _parse_target_item(platform: str, item: str | int | dict, defaults: dict | None = None) -> TargetConfig:
    if isinstance(item, str):
        payload = {"username": item}
    elif isinstance(item, int) and not isinstance(item, bool):
        payload = {"username": str(item)}
    elif isinstance(item, dict):
        payload = dict(item)
    else:
        raise ConfigError(
            f"Invalid target entry for {platform}: expected a username string, integer ID, or mapping, got {type(item).__name__}"
        )

    merged = {**(defaults or {}), **payload}
    username = merged.get("username") or merged.get("profile_url") or merged.get("url")
    if not username:
        raise ConfigError(f"Target entry for {platform} is missing required field: username")
    if platform == "facebook":
        username = normalize_facebook_profile_identifier(username)

    using_account = str(merged.get("using_account") or "").strip()
    using_accounts = _normalise_target_account_pool(merged.get("using_accounts"))

    return TargetConfig(
        platform=platform,
        username=username,
        check_posts=merged.get("check_posts", True),
        check_stories=merged.get("check_stories", True),
        check_highlights=merged.get("check_highlights", False),
        check_mentions=merged.get("check_mentions", False),
        using_account=using_account,
        using_accounts=using_accounts,
    )


def _parse_targets(raw: dict) -> list[TargetConfig]:
    """
    Support both the original verbose format:

        targets:
          instagram:
            - username: alice
              check_posts: true
              ...

    And the shorter defaults/items format:

        targets:
          instagram:
            defaults:
              check_posts: true
              ...
            items:
              - alice
              - username: bob
                check_mentions: false
    """
    result = []
    for platform, entries in raw.items():
        if isinstance(entries, list):
            for item in entries:
                result.append(_parse_target_item(platform, item))
            continue

        if isinstance(entries, dict):
            defaults = entries.get("defaults") or {}
            items = entries.get("items")
            if items is None:
                items = entries.get("targets")
            if items is None:
                raise ConfigError(
                    f"Invalid targets.{platform} section: expected 'items' list or legacy list format"
                )
            if not isinstance(items, list):
                raise ConfigError(
                    f"Invalid targets.{platform}.items: expected a list, got {type(items).__name__}"
                )
            for item in items:
                result.append(_parse_target_item(platform, item, defaults))
            continue

        raise ConfigError(
            f"Invalid targets.{platform} section: expected a list or mapping, got {type(entries).__name__}"
        )

    return result


def _load_credentials(accounts: list[AccountConfig]) -> dict[tuple[str, str], str]:
    credentials: dict[tuple[str, str], str] = {}
    missing = []
    for acc in accounts:
        key = _credential_env_key(acc.platform, acc.username)
        value = _clean_env_value(os.environ.get(key))
        if not value:
            missing.append(key)
        else:
            credentials[(acc.platform, acc.username)] = value
    if missing:
        raise ConfigError(
            f"Missing required environment variables for account passwords: {', '.join(missing)}\n"
            "Add them to your .env file. See .env.example for the naming convention."
        )
    return credentials


def _choose_target_account(platform: str, username: str, account_pool: list[str]) -> str:
    """Pick a stable account for a target from its allowed account pool."""
    if not account_pool:
        return ""
    if len(account_pool) == 1:
        return account_pool[0]

    digest = hashlib.sha256(f"{platform}:{username}".encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(account_pool)
    return account_pool[index]


def _normalise_target_accounts(
    targets: list[TargetConfig],
    accounts: list[AccountConfig],
) -> list[TargetConfig]:
    """
    Resolve target account routing against the configured account list.

    Priority order:
    1. `using_accounts` => stable automatic routing within that pool.
    2. `using_account`  => pin the target to one account.
    3. no override      => use the only platform account, or distribute across
       all configured platform accounts when multiple exist.
    """
    platform_accounts: dict[str, list[str]] = {}
    for account in accounts:
        platform_accounts.setdefault(account.platform, [])
        if account.username not in platform_accounts[account.platform]:
            platform_accounts[account.platform].append(account.username)

    normalised: list[TargetConfig] = []
    for target in targets:
        available_accounts = platform_accounts.get(target.platform, [])
        requested_account = (target.using_account or "").strip()
        requested_pool = [username for username in target.using_accounts if username.strip()]

        if requested_pool:
            if available_accounts:
                unknown_accounts = [
                    username for username in requested_pool if username not in available_accounts
                ]
                if unknown_accounts:
                    if len(available_accounts) == 1:
                        resolved_pool = [available_accounts[0]]
                    else:
                        raise ConfigError(
                            f"Target {target.platform}:{target.username} references unknown using_accounts "
                            f"{unknown_accounts!r}. Configured {target.platform} accounts: "
                            f"{', '.join(available_accounts)}"
                        )
                else:
                    resolved_pool = requested_pool
            else:
                resolved_pool = requested_pool
        elif requested_account:
            if requested_account in available_accounts:
                resolved_pool = [requested_account]
            elif len(available_accounts) == 1:
                resolved_pool = [available_accounts[0]]
            elif available_accounts:
                raise ConfigError(
                    f"Target {target.platform}:{target.username} references unknown using_account "
                    f"{requested_account!r}. Configured {target.platform} accounts: "
                    f"{', '.join(available_accounts)}"
                )
            else:
                resolved_pool = [requested_account]
        else:
            resolved_pool = list(available_accounts)

        resolved_account = _choose_target_account(
            target.platform,
            target.username,
            resolved_pool,
        )

        normalised.append(
            TargetConfig(
                platform=target.platform,
                username=target.username,
                check_posts=target.check_posts,
                check_stories=target.check_stories,
                check_highlights=target.check_highlights,
                check_mentions=target.check_mentions,
                using_account=resolved_account,
                using_accounts=resolved_pool,
            )
        )
    return normalised


def _from_dict(cls, data: dict):
    """Construct a dataclass from a dict, ignoring unknown keys."""
    import dataclasses
    valid_keys = {f.name for f in dataclasses.fields(cls)}
    filtered = {k: v for k, v in data.items() if k in valid_keys}
    return cls(**filtered)


def _parse_initiation_rule(raw_rule: dict) -> InitiationRuleConfig:
    """Normalize one appropriate-initiation rule from settings.yaml."""
    story_keywords = raw_rule.get("story_keywords")
    if story_keywords is None:
        story_keywords = raw_rule.get("keywords")
    if story_keywords is None:
        story_keywords = []

    return InitiationRuleConfig(
        platform=str(raw_rule.get("platform") or "").strip().lower(),
        username=str(raw_rule.get("username") or "").strip(),
        priority=str(raw_rule.get("priority") or "standard").strip().lower(),
        story_keywords=[
            str(keyword).strip()
            for keyword in (story_keywords or [])
            if str(keyword).strip()
        ],
        trigger_categories=[
            str(category).strip().lower()
            for category in (raw_rule.get("trigger_categories") or ["story_keyword"])
            if str(category).strip()
        ],
        cooldown_hours=raw_rule.get("cooldown_hours"),
        max_pending_drafts=raw_rule.get("max_pending_drafts"),
    )


def _parse_digital_twin(raw: dict | None) -> DigitalTwinConfig:
    """Build the DigitalTwinConfig dataclass with nested initiation rules."""
    payload = dict(raw or {})
    config = _from_dict(DigitalTwinConfig, payload)

    config.story_keyword_rules = [
        _parse_initiation_rule(rule)
        for rule in (payload.get("story_keyword_rules") or [])
        if isinstance(rule, dict)
    ]

    appropriate_initiation = payload.get("appropriate_initiation") or {}
    config.appropriate_initiation_rules = [
        _parse_initiation_rule(rule)
        for rule in (appropriate_initiation.get("targets") or [])
        if isinstance(rule, dict)
    ]
    return config


# ─── Public API ──────────────────────────────────────────────────────────────

def _resolve_config_relative_path(value: str | os.PathLike[str], base_dir: Path) -> Path:
    """Resolve config-defined relative paths against the settings.yaml directory."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_config(config_path: Path, env_path: Path) -> Config:
    """Parse settings.yaml and .env, validate, and return a populated Config."""
    config_path = config_path.resolve()
    if not config_path.exists():
        raise ConfigError(f"settings.yaml not found at: {config_path}")

    load_dotenv(env_path, override=False)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    config_dir = config_path.parent
    app_raw = raw.get("app", {})
    data_dir = _resolve_config_relative_path(
        os.environ.get("DATA_DIR", app_raw.get("data_dir", "./data")),
        config_dir,
    )
    log_level = os.environ.get("LOG_LEVEL", app_raw.get("log_level", "INFO"))
    dry_run = app_raw.get("dry_run", False)

    schedule = _from_dict(ScheduleConfig, raw.get("schedule", {}))
    stealth = _from_dict(StealthConfig, raw.get("stealth", {}))
    media_raw = raw.get("media", {})
    media = _from_dict(MediaConfig, media_raw)
    archive = _from_dict(ArchiveConfig, raw.get("archive", {}))
    logging_cfg = _from_dict(LoggingConfig, raw.get("logging", {}))
    logging_cfg.file = str(_resolve_config_relative_path(logging_cfg.file, config_dir))
    digital_twin = _parse_digital_twin(raw.get("digital_twin"))
    proxies_raw = raw.get("proxies", {})
    proxies = ProxyConfig(
        rotation=proxies_raw.get("rotation", "round_robin"),
        pool=proxies_raw.get("pool") or [],
    )

    accounts = _parse_accounts(raw.get("accounts") or [])
    targets = _normalise_target_accounts(
        _parse_targets(raw.get("targets") or {}),
        accounts,
    )

    telegram_bot_token = _clean_env_value(os.environ.get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id = _clean_env_value(os.environ.get("TELEGRAM_CHAT_ID"))
    gemini_api_key = _clean_env_value(os.environ.get("GEMINI_API_KEY"))
    meta_graph_access_token = _clean_env_value(os.environ.get("META_GRAPH_ACCESS_TOKEN"))
    meta_graph_api_version = _clean_env_value(os.environ.get("META_GRAPH_API_VERSION")) or "v22.0"
    meta_graph_base_url = _clean_env_value(os.environ.get("META_GRAPH_BASE_URL")) or "https://graph.instagram.com"
    meta_graph_app_secret = _clean_env_value(os.environ.get("META_GRAPH_APP_SECRET"))
    meta_graph_ig_user_id = _clean_env_value(os.environ.get("META_GRAPH_IG_USER_ID"))
    telegram_general_thread_id = _load_optional_int_env(
        "TELEGRAM_GENERAL_THREAD_ID",
        "TELEGRAM_STARTUP_THREAD_ID",
        "TELEGRAM_DIGEST_THREAD_ID",
    )
    telegram_digital_twin_thread_id = _load_optional_int_env("TELEGRAM_DIGITAL_TWIN_THREAD_ID")
    telegram_instagram_posts_thread_id = _load_optional_int_env(
        "TELEGRAM_INSTAGRAM_POSTS_THREAD_ID",
        "TELEGRAM_FEED_THREAD_ID",
    )
    telegram_instagram_mentions_thread_id = _load_optional_int_env("TELEGRAM_INSTAGRAM_MENTIONS_THREAD_ID")
    telegram_instagram_reposts_thread_id = _load_optional_int_env("TELEGRAM_INSTAGRAM_REPOSTS_THREAD_ID")
    telegram_instagram_stories_thread_id = _load_optional_int_env(
        "TELEGRAM_INSTAGRAM_STORIES_THREAD_ID",
        "TELEGRAM_STORIES_THREAD_ID",
    )
    telegram_instagram_highlights_thread_id = _load_optional_int_env("TELEGRAM_INSTAGRAM_HIGHLIGHTS_THREAD_ID")
    telegram_instagram_followers_thread_id = _load_optional_int_env(
        "TELEGRAM_INSTAGRAM_FOLLOWERS_THREAD_ID",
        "TELEGRAM_FOLLOWERS_THREAD_ID",
    )
    telegram_tiktok_posts_thread_id = _load_optional_int_env(
        "TELEGRAM_TIKTOK_POSTS_THREAD_ID",
        "TELEGRAM_FEED_THREAD_ID",
    )
    telegram_tiktok_reposts_thread_id = _load_optional_int_env("TELEGRAM_TIKTOK_REPOSTS_THREAD_ID")
    telegram_tiktok_stories_thread_id = _load_optional_int_env(
        "TELEGRAM_TIKTOK_STORIES_THREAD_ID",
        "TELEGRAM_STORIES_THREAD_ID",
    )
    telegram_tiktok_followers_thread_id = _load_optional_int_env(
        "TELEGRAM_TIKTOK_FOLLOWERS_THREAD_ID",
        "TELEGRAM_FOLLOWERS_THREAD_ID",
    )
    telegram_facebook_posts_thread_id = _load_optional_int_env(
        "TELEGRAM_FACEBOOK_POSTS_THREAD_ID",
        "TELEGRAM_FEED_THREAD_ID",
    )
    telegram_facebook_stories_thread_id = _load_optional_int_env(
        "TELEGRAM_FACEBOOK_STORIES_THREAD_ID",
        "TELEGRAM_STORIES_THREAD_ID",
    )
    telegram_facebook_friends_thread_id = _load_optional_int_env("TELEGRAM_FACEBOOK_FRIENDS_THREAD_ID")
    telegram_followers_thread_id = _load_optional_int_env("TELEGRAM_FOLLOWERS_THREAD_ID")
    telegram_errors_thread_id = _load_optional_int_env("TELEGRAM_ERRORS_THREAD_ID")

    notif_raw = raw.get("notifications", {})
    notifications = _from_dict(NotificationsConfig, notif_raw)
    notifications.chat_id = telegram_chat_id
    notifications.general_thread_id = telegram_general_thread_id
    notifications.instagram_posts_thread_id = telegram_instagram_posts_thread_id
    notifications.instagram_mentions_thread_id = telegram_instagram_mentions_thread_id
    notifications.instagram_reposts_thread_id = telegram_instagram_reposts_thread_id
    notifications.instagram_stories_thread_id = telegram_instagram_stories_thread_id
    notifications.instagram_highlights_thread_id = telegram_instagram_highlights_thread_id
    notifications.instagram_followers_thread_id = telegram_instagram_followers_thread_id
    notifications.tiktok_posts_thread_id = telegram_tiktok_posts_thread_id
    notifications.tiktok_reposts_thread_id = telegram_tiktok_reposts_thread_id
    notifications.tiktok_stories_thread_id = telegram_tiktok_stories_thread_id
    notifications.tiktok_followers_thread_id = telegram_tiktok_followers_thread_id
    notifications.facebook_posts_thread_id = telegram_facebook_posts_thread_id
    notifications.facebook_stories_thread_id = telegram_facebook_stories_thread_id
    notifications.facebook_friends_thread_id = telegram_facebook_friends_thread_id
    notifications.followers_thread_id = telegram_followers_thread_id
    notifications.errors_thread_id = telegram_errors_thread_id
    notifications.digital_twin_thread_id = telegram_digital_twin_thread_id

    # Validate required secrets
    missing_secrets = []
    if not telegram_bot_token:
        missing_secrets.append("TELEGRAM_BOT_TOKEN")
    if not telegram_chat_id:
        missing_secrets.append("TELEGRAM_CHAT_ID")
    if missing_secrets:
        raise ConfigError(
            f"Missing required environment variables: {', '.join(missing_secrets)}\n"
            "Add them to your .env file."
        )

    credentials = _load_credentials(accounts)

    # Derive paths from data_dir
    db_path = data_dir / "social_scanner.db"
    sessions_dir = data_dir / "sessions"
    media_dir = data_dir / "media"
    profiles_dir = data_dir / "profiles"
    accounts_dir = data_dir / "accounts"

    # Create directories so first run doesn't fail
    for d in [data_dir, sessions_dir, media_dir, profiles_dir, accounts_dir]:
        d.mkdir(parents=True, exist_ok=True)

    return Config(
        log_level=log_level,
        dry_run=dry_run,
        schedule=schedule,
        stealth=stealth,
        media=media,
        archive=archive,
        notifications=notifications,
        digital_twin=digital_twin,
        proxies=proxies,
        logging=logging_cfg,
        accounts=accounts,
        targets=targets,
        telegram_bot_token=telegram_bot_token,
        gemini_api_key=gemini_api_key,
        meta_graph_access_token=meta_graph_access_token,
        meta_graph_api_version=meta_graph_api_version,
        meta_graph_base_url=meta_graph_base_url,
        meta_graph_app_secret=meta_graph_app_secret,
        meta_graph_ig_user_id=meta_graph_ig_user_id,
        credentials=credentials,
        data_dir=data_dir,
        db_path=db_path,
        sessions_dir=sessions_dir,
        media_dir=media_dir,
        profiles_dir=profiles_dir,
        accounts_dir=accounts_dir,
    )
