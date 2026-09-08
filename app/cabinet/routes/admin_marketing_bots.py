"""Admin routes for managing marketing bots."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MarketingBot, User
from app.services.marketing_bot_manager import marketing_bot_manager

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/marketing-bots', tags=['Cabinet Admin Marketing Bots'])


# ============ Schemas ============


class MarketingBotCreate(BaseModel):
    name: str
    bot_token: str
    welcome_message: str
    image_url: str | None = None
    button_text: str | None = None
    button_url: str | None = None


class MarketingBotUpdate(BaseModel):
    name: str | None = None
    bot_token: str | None = None
    welcome_message: str | None = None
    image_url: str | None = None
    button_text: str | None = None
    button_url: str | None = None
    is_active: bool | None = None


class MarketingBotResponse(BaseModel):
    id: int
    name: str
    bot_token: str
    welcome_message: str
    image_url: str | None
    button_text: str | None
    button_url: str | None
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


# ============ Endpoints ============


@router.get('/', response_model=list[MarketingBotResponse])
async def list_marketing_bots(
    admin: User = Depends(require_permission('marketing_bots:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get all marketing bots."""
    try:
        result = await db.execute(select(MarketingBot).order_by(MarketingBot.id))
        bots = result.scalars().all()
        return [
            MarketingBotResponse(
                id=bot.id,
                name=bot.name,
                bot_token=bot.bot_token,
                welcome_message=bot.welcome_message,
                image_url=bot.image_url,
                button_text=bot.button_text,
                button_url=bot.button_url,
                is_active=bot.is_active,
                created_at=bot.created_at.isoformat(),
                updated_at=bot.updated_at.isoformat(),
            )
            for bot in bots
        ]
    except Exception as e:
        logger.error('Failed to list marketing bots', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load marketing bots',
        )


@router.get('/{bot_id}', response_model=MarketingBotResponse)
async def get_marketing_bot(
    bot_id: int,
    admin: User = Depends(require_permission('marketing_bots:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single marketing bot by ID."""
    try:
        result = await db.execute(select(MarketingBot).where(MarketingBot.id == bot_id))
        bot = result.scalar_one_or_none()

        if not bot:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Marketing bot not found',
            )

        return MarketingBotResponse(
            id=bot.id,
            name=bot.name,
            bot_token=bot.bot_token,
            welcome_message=bot.welcome_message,
            image_url=bot.image_url,
            button_text=bot.button_text,
            button_url=bot.button_url,
            is_active=bot.is_active,
            created_at=bot.created_at.isoformat(),
            updated_at=bot.updated_at.isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get marketing bot', bot_id=bot_id, error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load marketing bot',
        )


@router.post('/', response_model=MarketingBotResponse, status_code=status.HTTP_201_CREATED)
async def create_marketing_bot(
    data: MarketingBotCreate,
    admin: User = Depends(require_permission('marketing_bots:write')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new marketing bot."""
    try:
        bot = MarketingBot(
            name=data.name,
            bot_token=data.bot_token,
            welcome_message=data.welcome_message,
            image_url=data.image_url,
            button_text=data.button_text,
            button_url=data.button_url,
            is_active=True,
        )
        db.add(bot)
        await db.commit()
        await db.refresh(bot)

        # Start the bot
        await marketing_bot_manager.add_bot(bot)

        logger.info('Marketing bot created', bot_id=bot.id, name=bot.name, admin_id=admin.id)

        return MarketingBotResponse(
            id=bot.id,
            name=bot.name,
            bot_token=bot.bot_token,
            welcome_message=bot.welcome_message,
            image_url=bot.image_url,
            button_text=bot.button_text,
            button_url=bot.button_url,
            is_active=bot.is_active,
            created_at=bot.created_at.isoformat(),
            updated_at=bot.updated_at.isoformat(),
        )
    except Exception as e:
        await db.rollback()
        logger.error('Failed to create marketing bot', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create marketing bot',
        )


@router.patch('/{bot_id}', response_model=MarketingBotResponse)
async def update_marketing_bot(
    bot_id: int,
    data: MarketingBotUpdate,
    admin: User = Depends(require_permission('marketing_bots:write')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing marketing bot."""
    try:
        result = await db.execute(select(MarketingBot).where(MarketingBot.id == bot_id))
        bot = result.scalar_one_or_none()

        if not bot:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Marketing bot not found',
            )

        # Update fields
        if data.name is not None:
            bot.name = data.name
        if data.bot_token is not None:
            bot.bot_token = data.bot_token
        if data.welcome_message is not None:
            bot.welcome_message = data.welcome_message
        if data.image_url is not None:
            bot.image_url = data.image_url
        if data.button_text is not None:
            bot.button_text = data.button_text
        if data.button_url is not None:
            bot.button_url = data.button_url
        if data.is_active is not None:
            bot.is_active = data.is_active

        await db.commit()
        await db.refresh(bot)

        # Update running instance
        await marketing_bot_manager.update_bot(bot)

        logger.info('Marketing bot updated', bot_id=bot.id, name=bot.name, admin_id=admin.id)

        return MarketingBotResponse(
            id=bot.id,
            name=bot.name,
            bot_token=bot.bot_token,
            welcome_message=bot.welcome_message,
            image_url=bot.image_url,
            button_text=bot.button_text,
            button_url=bot.button_url,
            is_active=bot.is_active,
            created_at=bot.created_at.isoformat(),
            updated_at=bot.updated_at.isoformat(),
        )
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error('Failed to update marketing bot', bot_id=bot_id, error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update marketing bot',
        )


@router.delete('/{bot_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_marketing_bot(
    bot_id: int,
    admin: User = Depends(require_permission('marketing_bots:write')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a marketing bot."""
    try:
        result = await db.execute(select(MarketingBot).where(MarketingBot.id == bot_id))
        bot = result.scalar_one_or_none()

        if not bot:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Marketing bot not found',
            )

        # Stop the bot first
        await marketing_bot_manager.remove_bot(bot_id)

        await db.delete(bot)
        await db.commit()

        logger.info('Marketing bot deleted', bot_id=bot_id, admin_id=admin.id)

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error('Failed to delete marketing bot', bot_id=bot_id, error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete marketing bot',
        )
