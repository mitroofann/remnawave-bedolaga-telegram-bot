# Frontend Task: Marketing Bots Management UI

## Context

Backend уже реализован и готов. Добавлена система управления маркетинговыми Telegram-ботами — это отдельные боты, которые отвечают на любое сообщение приветствием с картинкой. Они работают независимо от основного бота приложения.

**Важно**: Это форк проекта. Все изменения делай **merge-safe**, чтобы в дальнейшем без конфликтов подтягивать изменения из апстрима.

## Backend API

### Endpoints

Все эндпоинты находятся по пути `/cabinet/admin/marketing-bots`:

```typescript
// List all bots
GET /cabinet/admin/marketing-bots
Response: MarketingBot[]

// Get single bot
GET /cabinet/admin/marketing-bots/{id}
Response: MarketingBot

// Create bot
POST /cabinet/admin/marketing-bots
Body: MarketingBotCreate
Response: MarketingBot

// Update bot
PATCH /cabinet/admin/marketing-bots/{id}
Body: MarketingBotUpdate
Response: MarketingBot

// Delete bot
DELETE /cabinet/admin/marketing-bots/{id}
Response: 204 No Content
```

### TypeScript Types

```typescript
interface MarketingBot {
  id: number;
  name: string;
  bot_token: string;
  welcome_message: string;
  image_url: string | null;
  button_text: string | null;
  button_url: string | null;
  is_active: boolean;
  created_at: string; // ISO datetime
  updated_at: string; // ISO datetime
}

interface MarketingBotCreate {
  name: string;
  bot_token: string;
  welcome_message: string;
  image_url?: string | null;
  button_text?: string | null;
  button_url?: string | null;
}

interface MarketingBotUpdate {
  name?: string;
  bot_token?: string;
  welcome_message?: string;
  image_url?: string | null;
  button_text?: string | null;
  button_url?: string | null;
  is_active?: boolean;
}
```

### Permissions

- `marketing_bots:read` - просмотр списка и деталей ботов
- `marketing_bots:write` - создание, редактирование и удаление ботов

## Requirements

### 1. Добавить категорию в меню админки

В разделе "Маркетинг" (Marketing) добавить новый пункт меню:
- **Название**: "Маркетинговые боты" / "Marketing Bots"
- **Иконка**: подходящая иконка бота (например, `MessageSquareBot` или аналог)
- **Путь**: `/admin/marketing-bots`

### 2. Страница списка ботов (`/admin/marketing-bots`)

**Макет страницы:**
- Заголовок: "Маркетинговые боты"
- Кнопка "Добавить бота" (справа от заголовка)
- Таблица со списком ботов

**Колонки таблицы:**
- **Название** (`name`)
- **Статус** (`is_active`) - бейдж: зеленый "Активен" / серый "Неактивен"
- **Токен** (`bot_token`) - показывать замаскированным (например, `123456:ABC...xyz` → `123456:ABC***xyz`)
- **Создан** (`created_at`) - форматированная дата
- **Действия** - кнопки "Редактировать" и "Удалить"

**Функциональность:**
- Загрузка списка при монтировании компонента
- Клик на "Добавить бота" → открыть модальное окно создания
- Клик на "Редактировать" → открыть модальное окно редактирования
- Клик на "Удалить" → показать подтверждение, затем удалить

### 3. Модальное окно создания/редактирования

**Поля формы:**

1. **Название бота** (`name`)
   - Тип: text input
   - Обязательное
   - Placeholder: "Например: Промо-бот осенняя акция"

2. **Токен бота** (`bot_token`)
   - Тип: text input
   - Обязательное
   - Placeholder: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
   - Hint: "Получите токен у @BotFather в Telegram"

3. **URL картинки** (`image_url`)
   - Тип: text input
   - Необязательное
   - Placeholder: "https://example.com/image.jpg"
   - Hint: "Прямая ссылка на изображение (необязательно)"

4. **Приветственное сообщение** (`welcome_message`)
   - Тип: textarea (минимум 4-5 строк высотой)
   - Обязательное
   - Placeholder: "Добро пожаловать! 🎉\n\nУзнайте больше по ссылке..."
   - Поддерживает HTML/Markdown форматирование

5. **Текст кнопки** (`button_text`)
   - Тип: text input
   - Необязательное
   - Placeholder: "Перейти в основной бот"
   - Hint: "Текст кнопки-ссылки под сообщением (необязательно)"

6. **URL кнопки** (`button_url`)
   - Тип: text input
   - Необязательное
   - Placeholder: "https://t.me/your_main_bot"
   - Hint: "Ссылка для кнопки (необязательно, требует текст кнопки)"

7. **Статус** (`is_active`) - только при редактировании
   - Тип: checkbox или toggle
   - Label: "Активен"

**Инструкция по форматированию:**
Разместить под полем `welcome_message` небольшую справку с примерами:

```
Поддерживается HTML-форматирование:
• <b>жирный</b> или <strong>жирный</strong>
• <i>курсив</i> или <em>курсив</em>
• <code>моноширинный</code>
• <a href="URL">ссылка</a>
```

Можно оформить как collapsible блок или tooltip.

**Валидация:**
- `name`: не пустое, макс 255 символов
- `bot_token`: не пустое, формат токена Telegram (regex: `^\d+:[A-Za-z0-9_-]{35}$`)
- `welcome_message`: не пустое
- `image_url`: если задан, должен быть валидным URL
- `button_text` и `button_url`: если задан один, второй тоже должен быть задан (работают только в паре)
- `button_url`: если задан, должен быть валидным URL

**Кнопки:**
- "Отмена" - закрыть модалку
- "Сохранить" / "Создать" - отправить данные на backend

### 4. Обработка ошибок

- Показывать toast-уведомления при успешном создании/обновлении/удалении
- Показывать ошибки валидации под соответствующими полями
- Обрабатывать сетевые ошибки с понятными сообщениями

### 5. Состояния загрузки

- Показывать skeleton/spinner при загрузке списка
- Disabled состояние кнопок во время отправки формы
- Loading состояние в таблице при удалении

## Merge-Safe подход

### ✅ Делай:
- Создай отдельный файл роута `/admin/marketing-bots`
- Используй существующие UI-компоненты проекта (таблицы, модалки, формы, кнопки)
- Следуй существующему стилю кода и структуре папок
- Добавь новый пункт в меню, не меняя существующие
- Используй те же паттерны API-запросов, что и в других админских страницах
- Используй существующую систему permissions/RBAC

### ❌ Не делай:
- Не меняй структуру существующих компонентов
- Не создавай новые shared-компоненты, если есть подходящие
- Не меняй стили глобально

## Примеры для вдохновения

Посмотри на реализацию других админских страниц в проекте:
- `/admin/campaigns` - для понимания структуры CRUD-страницы
- `/admin/broadcasts` - для примера работы с textarea и форматированием
- Другие `/admin/*` страницы для консистентности UI

## Структура файлов (примерная)

```
src/
  pages/
    admin/
      marketing-bots/
        index.tsx             # Страница списка
        MarketingBotModal.tsx # Модалка создания/редактирования
        types.ts              # TypeScript types
  api/
    marketing-bots.ts         # API-клиент
  components/
    admin/
      sidebar/                # Добавить пункт меню здесь
```

## Локализация

Если в проекте используется i18n:
- Добавь ключи для всех текстов в соответствующие locale-файлы
- Используй существующую систему локализации

Если локализации нет:
- Используй русский язык для текстов (или тот, что используется в проекте)

## Тестирование

После реализации проверь:
1. Список загружается корректно
2. Можно создать нового бота
3. Можно отредактировать существующего
4. Можно удалить бота (с подтверждением)
5. Валидация работает
6. Токен маскируется в таблице
7. Статус отображается корректно (активен/неактивен)
8. Инструкция по форматированию доступна и понятна

## Дополнительно

Если есть сомнения по дизайну или структуре — спроси у пользователя. Главное — сделать merge-safe и консистентно с остальным проектом.
