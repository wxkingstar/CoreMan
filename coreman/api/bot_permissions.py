"""兼容导出；共享权限逻辑位于 core.bots.permissions。"""

from coreman.core.bots.permissions import (
    MANAGER_ROLES as MANAGER_ROLES,
)
from coreman.core.bots.permissions import (
    BotPermissions as BotPermissions,
)
from coreman.core.bots.permissions import (
    BotRole as BotRole,
)
from coreman.core.bots.permissions import (
    bot_role as bot_role,
)
from coreman.core.bots.permissions import (
    can_create_bot as can_create_bot,
)
from coreman.core.bots.permissions import (
    can_delete_bot as can_delete_bot,
)
from coreman.core.bots.permissions import (
    can_edit_bot as can_edit_bot,
)
from coreman.core.bots.permissions import (
    can_manage_members as can_manage_members,
)
from coreman.core.bots.permissions import (
    can_reassign_team as can_reassign_team,
)
from coreman.core.bots.permissions import (
    can_switch_relay as can_switch_relay,
)
from coreman.core.bots.permissions import (
    can_toggle_bot as can_toggle_bot,
)
from coreman.core.bots.permissions import (
    can_view_env_full as can_view_env_full,
)
from coreman.core.bots.permissions import (
    can_view_sensitive as can_view_sensitive,
)
from coreman.core.bots.permissions import (
    is_bot_admin as is_bot_admin,
)
from coreman.core.bots.permissions import (
    is_manager as is_manager,
)
from coreman.core.bots.permissions import (
    permissions_for as permissions_for,
)
from coreman.core.bots.permissions import (
    relay_allowed_for_bot as relay_allowed_for_bot,
)
from coreman.core.bots.permissions import (
    same_team as same_team,
)
