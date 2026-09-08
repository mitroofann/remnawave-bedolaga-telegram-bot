"""Маршруты маркетинговых ботов доступны с путём без хвостового слэша и со слэшем.

Cabinet-роутер объявлен с ``redirect_slashes=False`` (см. ``app/cabinet/routes/__init__.py``),
поэтому рассинхрон слэша между фронтом и декоратором даёт 404, а не 307-редирект:
создание бота уезжало в прод с 404 на ``POST /api/cabinet/admin/marketing-bots``,
потому что эндпоинт был объявлен как ``@router.post('/')``. Слэш-дубль держится
для совместимости с фронтом, который шлёт GET списка со слэшем.
"""

from fastapi import FastAPI


def _openapi_paths() -> dict[str, set[str]]:
    from app.cabinet.routes import router

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    return {path: {method.upper() for method in ops} for path, ops in schema.get('paths', {}).items()}


def test_marketing_bots_collection_without_trailing_slash():
    paths = _openapi_paths()
    assert paths.get('/cabinet/admin/marketing-bots') == {'GET', 'POST'}


def test_marketing_bots_collection_with_trailing_slash_is_hidden_duplicate():
    """Слэш-дубль не должен показываться в схеме API (там основная — без слэша)."""
    paths = _openapi_paths()
    assert '/cabinet/admin/marketing-bots/' not in paths


def test_marketing_bots_item_routes():
    paths = _openapi_paths()
    assert paths.get('/cabinet/admin/marketing-bots/{bot_id}') == {'GET', 'PATCH', 'DELETE'}
