from app.models import AuditLog

async def add_audit(session, user_id, action, entity_type=None, entity_id=None, payload=None):
    session.add(AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload=payload,
    ))
