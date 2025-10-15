class DomainError(Exception):
    pass

class NotFound(DomainError):
    pass

class SlugTaken(DomainError):
    pass
