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
    SokobanCloseCurrentTab: function () {
        // Browsers only allow closing tabs that were opened by script. Try the
        // standard close first, then the same-window workaround used by Edge.
        window.close();
        if (!window.closed) {
            var currentWindow = window.open("", "_self");
            if (currentWindow) currentWindow.close();
        }
    },
    SokobanClearCoCreationPlayQuery: function () {
        var url = new URL(window.location.href);
        url.searchParams.delete('cocreationAttempt');
        url.searchParams.delete('cocreationPlay');
        window.history.replaceState({}, document.title, url.toString());
    }
});
