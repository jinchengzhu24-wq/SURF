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
