"""Errors shared by endpoint clients and runner policies."""


class PolicyError(RuntimeError):
    """The policy raised, or returned something the runner cannot use."""


class EndpointError(PolicyError):
    """The endpoint failed before a closed-choice answer was available."""


class DeadlineExceeded(EndpointError):
    """The absolute judgement deadline arrived before completion."""
