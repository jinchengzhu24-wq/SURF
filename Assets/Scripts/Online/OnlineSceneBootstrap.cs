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
        else if (scene.name == "Challenge_Waiting"
            && Object.FindObjectOfType<ChallengeWaitingController>() == null)
        {
            GameObject controller = new GameObject("ChallengeWaitingController");
            controller.AddComponent<ChallengeWaitingController>();
        }
        else if (scene.name == "Online_Level"
            && Object.FindObjectOfType<OnlineLevelController>() == null)
        {
            GameObject controller = new GameObject("OnlineLevelController");
            controller.AddComponent<OnlineLevelController>();
        }

        if ((scene.name == "PC_Level" || scene.name == "DG_Level")
            && Object.FindObjectOfType<OnlineChallengeCompletionRoute>() == null)
        {
            GameObject route = new GameObject("OnlineChallengeCompletionRoute");
            route.AddComponent<OnlineChallengeCompletionRoute>();
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

    public static void ConfigureRaycastTargets()
    {
        Graphic[] graphics = Object.FindObjectsOfType<Graphic>();

        for (int i = 0; i < graphics.Length; i++)
        {
            graphics[i].raycastTarget =
                graphics[i].GetComponentInParent<Selectable>() != null;
        }
    }
}
