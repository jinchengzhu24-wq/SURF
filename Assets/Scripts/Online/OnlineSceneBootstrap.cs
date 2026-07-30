using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class OnlineSceneBootstrap
{
    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    private static void Initialize()
    {
        SceneManager.sceneLoaded -= HandleSceneLoaded;
        SceneManager.sceneLoaded += HandleSceneLoaded;
        AttachController(SceneManager.GetActiveScene());
    }

    private static void HandleSceneLoaded(Scene scene, LoadSceneMode mode)
    {
        AttachController(scene);
    }

    private static void AttachController(Scene scene)
    {
        if (scene.name == "Online_Lobby"
            && Object.FindObjectOfType<OnlineLobbyController>() == null)
        {
            GameObject controller = new GameObject("OnlineLobbyController");
            controller.AddComponent<OnlineLobbyController>();
        }
        else if (scene.name == "Match_Briefing"
            && Object.FindObjectOfType<MatchBriefingController>() == null)
        {
            GameObject controller = new GameObject("MatchBriefingController");
            controller.AddComponent<MatchBriefingController>();
        }
    }
}

public static class OnlineSceneUi
{
    public static void EnsureEventSystem()
    {
        Canvas[] canvases = Object.FindObjectsOfType<Canvas>();

        for (int i = 0; i < canvases.Length; i++)
        {
            if (canvases[i].GetComponent<GraphicRaycaster>() == null)
            {
                canvases[i].gameObject.AddComponent<GraphicRaycaster>();
            }
        }

        if (Object.FindObjectOfType<EventSystem>() != null)
        {
            return;
        }

        new GameObject(
            "EventSystem",
            typeof(EventSystem),
            typeof(StandaloneInputModule)
        );
    }

    public static Button EnsureButton(string objectName)
    {
        GameObject target = GameObject.Find(objectName);

        if (target == null)
        {
            Debug.LogWarning("Online UI object was not found: " + objectName);
            return null;
        }

        Button button = target.GetComponent<Button>();

        if (button == null)
        {
            button = target.AddComponent<Button>();
        }

        if (button.targetGraphic == null)
        {
            button.targetGraphic = target.GetComponent<Graphic>();
        }

        return button;
    }

    public static Text FindText(string objectName)
    {
        GameObject target = GameObject.Find(objectName);
        return target != null ? target.GetComponent<Text>() : null;
    }
}
