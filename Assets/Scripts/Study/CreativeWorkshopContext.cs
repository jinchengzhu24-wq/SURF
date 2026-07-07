using UnityEngine;

public static class CreativeWorkshopContext
{
    public const string IdeaIdPrefsKey = "SokobanCreativeWorkshopIdeaId";
    public const string SessionIdPrefsKey = "SokobanCreativeWorkshopIdeaSessionId";
    public const string IdeaTextPrefsKey = "SokobanCreativeWorkshopIdeaText";

    public static string IdeaId { get; private set; }
    public static string SessionId { get; private set; }
    public static string IdeaText { get; private set; }

    public static bool HasIdea
    {
        get { return !string.IsNullOrEmpty(IdeaText); }
    }

    public static void SetIdea(string ideaId, string sessionId, string ideaText)
    {
        IdeaId = ideaId ?? "";
        SessionId = sessionId ?? "";
        IdeaText = ideaText ?? "";

        PlayerPrefs.SetString(IdeaIdPrefsKey, IdeaId);
        PlayerPrefs.SetString(SessionIdPrefsKey, SessionId);
        PlayerPrefs.SetString(IdeaTextPrefsKey, IdeaText);
        PlayerPrefs.Save();
    }

    public static void Clear()
    {
        IdeaId = "";
        SessionId = "";
        IdeaText = "";

        PlayerPrefs.DeleteKey(IdeaIdPrefsKey);
        PlayerPrefs.DeleteKey(SessionIdPrefsKey);
        PlayerPrefs.DeleteKey(IdeaTextPrefsKey);
        PlayerPrefs.Save();
    }
}
