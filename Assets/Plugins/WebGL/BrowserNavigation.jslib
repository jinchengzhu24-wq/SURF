mergeInto(LibraryManager.library, {
    SokobanNavigateCurrentPage: function (urlPointer) {
        window.location.assign(UTF8ToString(urlPointer));
    },
    SokobanOpenDashboardGate: function (urlPointer) {
        var url = UTF8ToString(urlPointer);
        if (typeof window.SokobanOpenDashboardGate === "function") {
            window.SokobanOpenDashboardGate(url);
        } else {
            window.open(url, "_blank", "noopener,noreferrer");
        }
    },
    SokobanClearCoCreationPlayQuery: function () {
        var url = new URL(window.location.href);
        url.searchParams.delete('cocreationAttempt');
        url.searchParams.delete('cocreationPlay');
        window.history.replaceState({}, document.title, url.toString());
    }
});
