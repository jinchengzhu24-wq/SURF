mergeInto(LibraryManager.library, {
    SokobanNavigateCurrentPage: function (urlPointer) {
        window.location.assign(UTF8ToString(urlPointer));
    },
    SokobanClearCoCreationPlayQuery: function () {
        var url = new URL(window.location.href);
        url.searchParams.delete('cocreationAttempt');
        url.searchParams.delete('cocreationPlay');
        window.history.replaceState({}, document.title, url.toString());
    }
});
