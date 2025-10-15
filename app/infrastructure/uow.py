from sqlalchemy.orm import Session
from app.domain.ports import UnitOfWork
from .repos import SqlPostRepo

class SqlAlchemyUoW(UnitOfWork):
    def __init__(self, session: Session):
        self.session = session
        self.posts = SqlPostRepo(session)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
