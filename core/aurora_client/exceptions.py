class AuroraError(Exception):
    pass

class ConnectionError(AuroraError):
    pass

class AuthenticationError(AuroraError):
    pass

class ProtocolError(AuroraError):
    pass

class TaskNotFoundError(AuroraError):
    pass

class FileNotFoundError(AuroraError):
    pass

class SaveError(AuroraError):
    pass

class ValidationError(AuroraError):
    pass
