mergeInto(LibraryManager.library, {
    SokobanNavigateCurrentPage: function (urlPointer) {
        window.location.assign(UTF8ToString(urlPointer));
    },
    SokobanClearCoCreationPlayQuery: function () {
        var url = new URL(window.location.href);
        url.searchParams.delete('cocreationAttempt');
        url.searchParams.delete('cocreationPlay');
        window.history.replaceState({}, document.title, url.toString());
    },
    SokobanSetCoCreationPlayBridgeReady: function (ready) {
        if (window.SokobanSetCoCreationPlayBridgeReady) {
            window.SokobanSetCoCreationPlayBridgeReady(ready !== 0);
        }
    },
    SokobanReturnToCoCreationLab: function (statusPointer) {
        if (window.SokobanReturnToCoCreationLab) {
            window.SokobanReturnToCoCreationLab(UTF8ToString(statusPointer));
        }
    },
    SokobanReturnDraftRegeneration: function (sessionIdPointer, requestIdPointer, statusPointer) {
        if (window.SokobanReturnDraftRegeneration) {
            window.SokobanReturnDraftRegeneration(
                UTF8ToString(sessionIdPointer),
                UTF8ToString(requestIdPointer),
                UTF8ToString(statusPointer)
            );
        }
    },
    SokobanSetDraftRegenerationBridgeSession: function (sessionIdPointer, protocolVersion) {
        if (window.SokobanSetDraftRegenerationBridgeSession) {
            window.SokobanSetDraftRegenerationBridgeSession(
                UTF8ToString(sessionIdPointer),
                protocolVersion
            );
        }
    },
    SokobanShowCoCreationLab: function (urlPointer, sessionIdPointer) {
        if (window.SokobanShowCoCreationLab) {
            window.SokobanShowCoCreationLab(
                UTF8ToString(urlPointer),
                UTF8ToString(sessionIdPointer)
            );
        }
    },
    SokobanLobbySetOverlayVisible: function (visible) {
        if (window.SokobanSetLobbyOverlayVisible) {
            window.SokobanSetLobbyOverlayVisible(visible !== 0);
        }
    },
    SokobanLobbySetRoomCode: function (roomCodePointer) {
        if (window.SokobanSetLobbyRoomCode) {
            window.SokobanSetLobbyRoomCode(UTF8ToString(roomCodePointer));
        }
    },
    SokobanLobbySetJoinCode: function (roomCodePointer) {
        if (window.SokobanSetLobbyJoinCode) {
            window.SokobanSetLobbyJoinCode(UTF8ToString(roomCodePointer));
        }
    }
});
