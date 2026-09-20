"""The composition boundary: what a releasable application is, and what a session may demand.

``descriptor`` holds the versioned facts of one release, ``ports`` the five executable
contracts, ``sources`` the offline-compiled knowledge a deployment may arm sessions
with, and ``binding`` the single checkpoint that validates the whole boundary before a
user task reaches the Agent loop. ``live`` runs the product lifecycle built on that
boundary, and ``operations`` backs up and restores the local state it accumulates.
Neither is re-exported here: they are run, not imported as contracts.
"""
from .binding import ApplicationAdapter, BoundApplication, bind_application
from .descriptor import APPLICATION_ADAPTER_VERSION, ApplicationDescriptor
from .ports import (AnonymousIdentityPort, ApplicationIdentityPort,
                    ApplicationMutationReconciler, ApplicationPorts, ApplicationRoutePort,
                    ConservativeMutationReconciler, DeclaredDataClassifier,
                    DeclaredRoutePort, MutationReconciliation, ResultPostconditionVerifier)
from .sources import SourceCapability, SourceCompiler

__all__ = ["APPLICATION_ADAPTER_VERSION", "AnonymousIdentityPort",
           "ApplicationAdapter", "ApplicationDescriptor", "ApplicationIdentityPort",
           "ApplicationMutationReconciler", "ApplicationPorts", "ApplicationRoutePort",
           "BoundApplication", "ConservativeMutationReconciler", "DeclaredDataClassifier",
           "DeclaredRoutePort", "MutationReconciliation", "ResultPostconditionVerifier",
           "SourceCapability", "SourceCompiler", "bind_application"]
