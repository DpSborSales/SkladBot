# handlers/__init__.py
from .common import register_common_handlers
from .edit_order import register_edit_handlers
from .transfer import register_transfer_handlers
from .payments import register_payment_handlers
from .admin import register_admin_handlers
from .direct_sale import register_direct_sale_handlers
from .packing import register_packing_handlers   # новый

def register_all_handlers(bot):
    register_common_handlers(bot)
    register_edit_handlers(bot)
    register_transfer_handlers(bot)
    register_payment_handlers(bot)
    register_admin_handlers(bot)
    register_direct_sale_handlers(bot)
    register_packing_handlers(bot)                # новый
