from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Базовый класс ORM-моделей. Конкретные модели добавляются с M1."""
