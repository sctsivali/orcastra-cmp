"""The audit catalogue: every action the console emits, its category and its severity.

Split out of `activity_logger` so that module stays under the house limit. The catalogue is
data; the ratchets in tests/ (`test_a_task_action_is_named_in_the_audit_catalogue`,
`test_a_schedule_action_is_named_in_the_audit_catalogue`, `test_proxy_destructive_actions_are_named`)
read it through `activity_logger`, which re-exports every name here.
"""

from enum import Enum


class EventCategory(str, Enum):
    """Event categories for audit logging."""
    IDENTITY = "identity"
    LXD_INFRA = "lxd_infra"
    STORAGE = "storage"
    NETWORK = "network"
    ADMIN = "admin"
    SETTINGS = "settings"
    VAULT = "vault"


class EventSeverity(str, Enum):
    """Event severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Action to category mapping
ACTION_CATEGORIES = {
    # Identity
    "auth.login": EventCategory.IDENTITY,
    "auth.logout": EventCategory.IDENTITY,
    "auth.token.refresh": EventCategory.IDENTITY,
    "user.ssh_key.create": EventCategory.IDENTITY,
    "user.ssh_key.delete": EventCategory.IDENTITY,
    
    # LXD Infrastructure
    "cluster.register": EventCategory.LXD_INFRA,
    "cluster.delete": EventCategory.LXD_INFRA,
    "cluster.auth_check": EventCategory.LXD_INFRA,
    "instance.create": EventCategory.LXD_INFRA,
    "instance.start": EventCategory.LXD_INFRA,
    "instance.stop": EventCategory.LXD_INFRA,
    "instance.restart": EventCategory.LXD_INFRA,
    "instance.delete": EventCategory.LXD_INFRA,
    "instance.migrate": EventCategory.LXD_INFRA,
    "instance.snapshot.create": EventCategory.LXD_INFRA,
    "instance.snapshot.restore": EventCategory.LXD_INFRA,
    "instance.snapshot.delete": EventCategory.LXD_INFRA,
    "instance.snapshot.billing_override": EventCategory.LXD_INFRA,
    # Orcastra's own recurring snapshots. Registered explicitly because an unregistered action
    # falls through to LXD_INFRA/MEDIUM silently, which is a category nobody chose.
    "instance.snapshot.schedule.create": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.update": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.delete": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.run": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.pruned": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.paused": EventCategory.LXD_INFRA,
    "instance.snapshot.schedule.missed": EventCategory.LXD_INFRA,
    "volume.snapshot.create": EventCategory.STORAGE,
    "volume.snapshot.restore": EventCategory.STORAGE,
    "volume.snapshot.delete": EventCategory.STORAGE,
    "vm.create": EventCategory.LXD_INFRA,
    "vm.start": EventCategory.LXD_INFRA,
    "vm.stop": EventCategory.LXD_INFRA,
    "vm.restart": EventCategory.LXD_INFRA,
    "vm.delete": EventCategory.LXD_INFRA,
    "project.create": EventCategory.LXD_INFRA,
    "project.delete": EventCategory.LXD_INFRA,
    "profile.create": EventCategory.LXD_INFRA,
    "profile.update": EventCategory.LXD_INFRA,
    "profile.delete": EventCategory.LXD_INFRA,
    "image.copy": EventCategory.LXD_INFRA,
    "image.delete": EventCategory.LXD_INFRA,
    
    # Storage
    "storage.pool.create": EventCategory.STORAGE,
    "storage.pool.delete": EventCategory.STORAGE,
    "storage.volume.create": EventCategory.STORAGE,
    "storage.volume.delete": EventCategory.STORAGE,
    "storage.volume.resize": EventCategory.STORAGE,
    
    # Network
    "network.create": EventCategory.NETWORK,
    "network.delete": EventCategory.NETWORK,
    "network.acl.create": EventCategory.NETWORK,
    "network.acl.delete": EventCategory.NETWORK,
    
    # Admin
    "terminal.session.create": EventCategory.ADMIN,
    "console.session.create": EventCategory.ADMIN,
    # The key-authenticated surface. `integration.instance.exec` and `integration.instance.files`
    # are the API-key equivalents of the two session actions above: they reach inside a guest.
    # `integration.proxy.request` covers everything else that goes through the raw proxy, which
    # had no audit record at all before, only an app log line that the deduplication filter
    # collapsed.
    "integration.instance.exec": EventCategory.ADMIN,
    "integration.instance.files": EventCategory.ADMIN,
    # Seeing and driving a guest's graphical session. Three names rather than one so an alert can
    # tell an agent looking at a screen from an agent typing into it: they are different events to
    # whoever reviews them, and only one of them can change anything.
    "desktop_control.consent.set": EventCategory.ADMIN,
    "integration.instance.desktop_view": EventCategory.ADMIN,
    "integration.instance.desktop_input": EventCategory.ADMIN,
    "integration.instance.desktop_clipboard": EventCategory.ADMIN,
    "integration.proxy.request": EventCategory.ADMIN,
    # A key setting the snapshot-schedule config keys. Not refused yet, so the record IS the
    # control: it is what says which keys write them before a refusal is fitted to the answer.
    "integration.instance.config.policy_key_write": EventCategory.ADMIN,
    # Credential lifecycle. The actor here is a human operator holding a JWT, not the key,
    # so these carry the operator in `actor` and the key only as an annotation in details.
    "integration.api_key.create": EventCategory.ADMIN,
    "integration.api_key.capabilities.update": EventCategory.ADMIN,
    "integration.api_key.revoke": EventCategory.ADMIN,
    # A new secret for the same key id. It hands the caller a working credential, so it ranks
    # with the grant-shaped actions beside it.
    "integration.api_key.secret.rotate": EventCategory.ADMIN,
    "integration.api_key.delete": EventCategory.ADMIN,
    # An integration push moving its creator's unbound cluster into an organization.
    "integration.cluster.rebind": EventCategory.ADMIN,
    # An Administrator moving a synced organization to another owner key. The owner is the only
    # key that may sync the organization, write its members and policies and provision for it.
    "integration.organization.owner.transfer": EventCategory.ADMIN,
    # Add-ons. Installing one hands a third party a credential into an organization, so the
    # grant-shaped actions are high severity and the descriptive ones are not.
    "extension.register": EventCategory.SETTINGS,
    "extension.update": EventCategory.SETTINGS,
    "extension.delete": EventCategory.SETTINGS,
    "extension.install": EventCategory.ADMIN,
    "extension.install.rescope": EventCategory.ADMIN,
    "extension.install.suspend": EventCategory.ADMIN,
    "extension.install.resume": EventCategory.ADMIN,
    "extension.install.credential.rotate": EventCategory.ADMIN,
    "extension.install.settings": EventCategory.SETTINGS,
    "extension.install.uninstall": EventCategory.ADMIN,
    "extension.install.scope_narrowed": EventCategory.ADMIN,
    "extension.credential.orphan_revoked": EventCategory.ADMIN,
    "extension.webhook.subscribe": EventCategory.SETTINGS,
    "extension.webhook.unsubscribe": EventCategory.SETTINGS,
    "extension.webhook.secret.rotate": EventCategory.ADMIN,
    "extension.webhook.redeliver": EventCategory.ADMIN,
    "extension.webhook.suspended": EventCategory.ADMIN,
    # A destructive proxy call removes the same resource the dashboard's own endpoint would, so
    # it is named for the act rather than for the transport. Without these it landed under
    # `integration.proxy.request`, which is not high severity, and an alert on `image.delete`
    # could never see the API-key route.
    "integration.image.delete": EventCategory.LXD_INFRA,
    "integration.instance.delete": EventCategory.LXD_INFRA,
    "integration.network.delete": EventCategory.LXD_INFRA,
    "integration.project.delete": EventCategory.LXD_INFRA,
    "integration.profile.delete": EventCategory.LXD_INFRA,
    "integration.certificate.delete": EventCategory.LXD_INFRA,
    "integration.storage.pool.delete": EventCategory.LXD_INFRA,
    "integration.storage.volume.delete": EventCategory.LXD_INFRA,
    "admin.audit.view": EventCategory.ADMIN,
    
    # Settings
    "user.preference.update": EventCategory.SETTINGS,
    "tag.create": EventCategory.SETTINGS,
    "tag.assign": EventCategory.SETTINGS,
    "tag.remove": EventCategory.SETTINGS,
    
    # Vault
    "vault.secret.read": EventCategory.VAULT,
    "vault.secret.write": EventCategory.VAULT,
    "vault.secret.delete": EventCategory.VAULT,
    "vault.cert.generate": EventCategory.VAULT,
    "vault.cert.list": EventCategory.VAULT,
    "vault.kv.put": EventCategory.VAULT,
    "vault.kv.list": EventCategory.VAULT,
    "vault.health.check": EventCategory.VAULT,

    # LXD service-layer operations (distinct from router-layer actions)
    "lxd.instance.create": EventCategory.LXD_INFRA,
    "lxd.instance.start": EventCategory.LXD_INFRA,
    "lxd.instance.stop": EventCategory.LXD_INFRA,
    "lxd.instance.restart": EventCategory.LXD_INFRA,
    "lxd.instance.delete": EventCategory.LXD_INFRA,
    "lxd.project.create": EventCategory.LXD_INFRA,
    "lxd.project.update": EventCategory.LXD_INFRA,
    "lxd.project.delete": EventCategory.LXD_INFRA,
    "lxd.image.copy": EventCategory.LXD_INFRA,
    "lxd.image.delete": EventCategory.LXD_INFRA,
    "lxd.connection.establish": EventCategory.LXD_INFRA,
    "lxd.connection.remove": EventCategory.LXD_INFRA,
    "lxd.connection.reconnect": EventCategory.LXD_INFRA,
    # Cluster management
    "cluster.location.update": EventCategory.ADMIN,

    # The task journal behind the bottom panel. Cancelling ends work someone else started, and
    # the follower's election and loss are the two facts an operator reads when the panel says
    # "Journal paused".
    "task.cancel": EventCategory.LXD_INFRA,
    "task.journal.prune": EventCategory.ADMIN,
    "task.journal.follower.elected": EventCategory.ADMIN,
    "task.journal.follower.lost": EventCategory.ADMIN,
    # Dashboard uploads had no audit record at all; these two are the accepted and terminal
    # records the journal turns into an upload task.
    "image.upload.import": EventCategory.LXD_INFRA,
    "storage.iso.upload.import": EventCategory.STORAGE,
    # Emitted for a long time without a registration, so they filed under LXD_INFRA/MEDIUM by
    # accident. Registered here so the cluster log's category column says what they are.
    "audit.view": EventCategory.ADMIN,
    "extension.version.publish": EventCategory.SETTINGS,
    "file.push": EventCategory.ADMIN,
    "instance.rebuild": EventCategory.LXD_INFRA,
    "instance.autostart": EventCategory.LXD_INFRA,
    "instance.nic.address": EventCategory.NETWORK,
    "integration.mcp.tool_call": EventCategory.ADMIN,
    "rbac.access_denied": EventCategory.IDENTITY,
}

# High severity actions
HIGH_SEVERITY_ACTIONS = {
    "cluster.register",
    "cluster.delete",
    "instance.delete",
    "instance.snapshot.restore",
    "instance.snapshot.delete",
    "instance.snapshot.billing_override",
    # `.pruned` destroys a restore point, so it ranks with instance.snapshot.delete beside it.
    # `.paused` and `.delete` are how an operator learns their backups stopped.
    "instance.snapshot.schedule.delete",
    "instance.snapshot.schedule.pruned",
    "instance.snapshot.schedule.paused",
    "volume.snapshot.restore",
    "volume.snapshot.delete",
    "vm.delete",
    "project.delete",
    "storage.pool.delete",
    "storage.volume.delete",
    "image.delete",
    "network.delete",
    "terminal.session.create",
    "console.session.create",
    "integration.instance.exec",
    "integration.instance.config.policy_key_write",
    "integration.instance.files",
    "desktop_control.consent.set",
    "integration.instance.desktop_view",
    "integration.instance.desktop_input",
    "integration.instance.desktop_clipboard",
    "integration.api_key.create",
    "integration.api_key.capabilities.update",
    "integration.api_key.revoke",
    "integration.api_key.secret.rotate",
    "integration.api_key.delete",
    "integration.cluster.rebind",
    "integration.organization.owner.transfer",
    "extension.install",
    "extension.install.rescope",
    "extension.install.resume",
    "extension.install.credential.rotate",
    "extension.credential.orphan_revoked",
    "extension.webhook.secret.rotate",
    "extension.webhook.redeliver",
    "integration.image.delete",
    "integration.instance.delete",
    "integration.network.delete",
    "integration.project.delete",
    "integration.profile.delete",
    "integration.certificate.delete",
    "integration.storage.pool.delete",
    "integration.storage.volume.delete",
    "break_glass.grant.create",
    "break_glass.grant.revoke",
    "user.ssh_key.create",
    "user.ssh_key.delete",
    "vault.cert.generate",
    "vault.secret.write",
    "vault.secret.delete",
    # Service-layer high severity
    "lxd.instance.delete",
    "lxd.project.delete",
    "lxd.connection.establish",
    "lxd.connection.remove",
    "lxd.connection.reconnect",
    # Ends work another operator started.
    "task.cancel",
    "instance.rebuild",
    "file.push",
}
