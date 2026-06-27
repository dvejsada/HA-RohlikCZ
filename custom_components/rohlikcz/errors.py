from homeassistant.exceptions import HomeAssistantError


class RohlikczError(HomeAssistantError):
    """ Base rohlik.cz integration error class. """


class NotAuthorizedError(RohlikczError):
    """ User is not authorized. """


class InvalidCredentialsError(RohlikczError):
    """ User provided wrong credentials. """


class AddressNotSetError(RohlikczError):
    """ No delivery address set in user account. """


class APIRequestFailedError(RohlikczError):
    """ An API request to Rohlik.cz failed (e.g. connection error). """
