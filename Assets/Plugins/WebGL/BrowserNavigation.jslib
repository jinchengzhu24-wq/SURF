mergeInto(LibraryManager.library, {
    SokobanNavigateCurrentPage: function (urlPointer) {
        window.location.assign(UTF8ToString(urlPointer));
    }
});
