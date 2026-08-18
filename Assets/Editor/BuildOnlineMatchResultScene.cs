#if UNITY_EDITOR
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class BuildOnlineMatchResultScene
{
    private const string ResultScenePath =
        "Assets/Scenes/Matchmaking/Online/Match_Result.unity";
    private const string OnlineLevelScenePath =
        "Assets/Scenes/Matchmaking/Online/Online_Level.unity";
    private const string QuestionnaireScenePath =
        "Assets/Scenes/Matchmaking/Online/Questionnaire(Online).unity";

    private static readonly Color Ink = new Color32(52, 67, 70, 255);
    private static readonly Color Cream = new Color32(255, 240, 211, 248);
    private static readonly Color Orange = new Color32(224, 129, 56, 255);
    private static readonly Color Green = new Color32(67, 155, 91, 255);
    private static readonly Color Blue = new Color32(82, 104, 177, 255);

    private static Font pixelFont;

    [MenuItem("Tools/Online/Build Match Result Scene")]
    public static void Build()
    {
        if (EditorApplication.isPlayingOrWillChangePlaymode)
        {
            return;
        }

        pixelFont = AssetDatabase.LoadAssetAtPath<Font>(
            "Assets/Font/Pixelnauts.ttf"
        );

        if (pixelFont == null)
        {
            throw new System.InvalidOperationException(
                "Pixelnauts font could not be loaded."
            );
        }

        BuildResultScene();
        ConfigureOnlineLevelPlayer();
        EnsureBuildSettings();
        AssetDatabase.SaveAssets();
        Debug.Log("Online Match_Result scene was built successfully.");
    }

    [MenuItem("Tools/Online/Fix Match Result Text Layout")]
    public static void FixResultTextLayout()
    {
        Scene previousActiveScene = SceneManager.GetActiveScene();
        Scene scene = SceneManager.GetSceneByPath(ResultScenePath);
        bool openedForFix = !scene.IsValid() || !scene.isLoaded;

        if (openedForFix)
        {
            scene = EditorSceneManager.OpenScene(
                ResultScenePath,
                OpenSceneMode.Additive
            );
        }

        SceneManager.SetActiveScene(scene);
        ConfigureResultCardTextLayout(
            FindNamedComponentInScene<Text>(scene, "OpponentRunTimeText"),
            FindNamedComponentInScene<Text>(scene, "OpponentRunMovesText"),
            FindNamedComponentInScene<Text>(scene, "OwnFeedbackText")
        );
        ConfigureResultCardTextLayout(
            FindNamedComponentInScene<Text>(scene, "OwnRunTimeText"),
            FindNamedComponentInScene<Text>(scene, "OwnRunMovesText"),
            FindNamedComponentInScene<Text>(scene, "OpponentFeedbackText")
        );
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        RestoreScene(previousActiveScene, scene, openedForFix);
        Debug.Log("Match_Result text layout was fixed successfully.");
    }

    private static void BuildResultScene()
    {
        Scene previousActiveScene = SceneManager.GetActiveScene();
        Scene scene = SceneManager.GetSceneByPath(ResultScenePath);
        bool openedForBuild = !scene.IsValid() || !scene.isLoaded;

        if (openedForBuild)
        {
            scene = EditorSceneManager.OpenScene(
                ResultScenePath,
                OpenSceneMode.Additive
            );
        }

        SceneManager.SetActiveScene(scene);
        DestroyRootIfPresent(scene, "MatchResultCanvas");
        DestroyRootIfPresent(scene, "MatchResultController");
        DestroyRootIfPresent(scene, "EventSystem");

        Canvas canvas = CreateCanvas("MatchResultCanvas");
        RectTransform root = CreateRect(
            "MatchResultUIRoot",
            canvas.transform,
            Vector2.zero,
            Vector2.zero
        );
        Stretch(root);

        CreateText(
            "MatchResultTitleText",
            root,
            "MATCH RESULT",
            62,
            Ink,
            new Vector2(0f, 430f),
            new Vector2(1200f, 90f),
            TextAnchor.MiddleCenter
        );
        Text roomCodeText = CreateText(
            "RoomCodeText",
            root,
            "ROOM ------",
            24,
            Blue,
            new Vector2(0f, 360f),
            new Vector2(700f, 50f),
            TextAnchor.MiddleCenter
        );
        Text statusText = CreateText(
            "MatchResultStatusText",
            root,
            "WAITING FOR OPPONENT TO FINISH...",
            27,
            Ink,
            new Vector2(0f, 305f),
            new Vector2(1400f, 60f),
            TextAnchor.MiddleCenter
        );

        Image ownCard = CreatePanel(
            "OwnChallengeResultCard",
            root,
            Cream,
            new Vector2(-425f, 25f),
            new Vector2(780f, 480f)
        );
        CreatePanel(
            "OwnCardAccent",
            ownCard.rectTransform,
            Blue,
            new Vector2(0f, 220f),
            new Vector2(780f, 40f)
        );
        CreateText(
            "OwnCardTitleText",
            ownCard.rectTransform,
            "YOUR CHALLENGE",
            34,
            Blue,
            new Vector2(0f, 158f),
            new Vector2(690f, 60f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "OwnCardSubtitleText",
            ownCard.rectTransform,
            "OPPONENT'S RUN",
            22,
            Ink,
            new Vector2(0f, 112f),
            new Vector2(690f, 45f),
            TextAnchor.MiddleCenter
        );
        Text ownAssistantText = CreateText(
            "OwnChallengeAssistantText",
            ownCard.rectTransform,
            "AI MODE  DESCRIPTION-TO-LEVEL",
            22,
            Ink,
            new Vector2(0f, 50f),
            new Vector2(690f, 48f),
            TextAnchor.MiddleLeft
        );
        Text opponentTimeText = CreateText(
            "OpponentRunTimeText",
            ownCard.rectTransform,
            "TIME  --",
            29,
            Orange,
            new Vector2(-190f, -4f),
            new Vector2(310f, 55f),
            TextAnchor.MiddleLeft
        );
        Text opponentMovesText = CreateText(
            "OpponentRunMovesText",
            ownCard.rectTransform,
            "MOVES  -- / MIN --",
            29,
            Orange,
            new Vector2(155f, -4f),
            new Vector2(380f, 55f),
            TextAnchor.MiddleLeft
        );
        Text ownFeedbackText = CreateText(
            "OwnFeedbackText",
            ownCard.rectTransform,
            "YOUR MESSAGE:  --",
            29,
            Blue,
            new Vector2(0f, -113f),
            new Vector2(690f, 145f),
            TextAnchor.UpperLeft
        );
        ConfigureFeedbackText(ownFeedbackText);

        Image opponentCard = CreatePanel(
            "OpponentChallengeResultCard",
            root,
            Cream,
            new Vector2(425f, 25f),
            new Vector2(780f, 480f)
        );
        CreatePanel(
            "OpponentCardAccent",
            opponentCard.rectTransform,
            Orange,
            new Vector2(0f, 220f),
            new Vector2(780f, 40f)
        );
        CreateText(
            "OpponentCardTitleText",
            opponentCard.rectTransform,
            "OPPONENT'S CHALLENGE",
            34,
            Orange,
            new Vector2(0f, 158f),
            new Vector2(690f, 60f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "OpponentCardSubtitleText",
            opponentCard.rectTransform,
            "YOUR RUN",
            22,
            Ink,
            new Vector2(0f, 112f),
            new Vector2(690f, 45f),
            TextAnchor.MiddleCenter
        );
        Text opponentAssistantText = CreateText(
            "OpponentChallengeAssistantText",
            opponentCard.rectTransform,
            "AI MODE  PARTIAL-LEVEL COMPLETION",
            22,
            Ink,
            new Vector2(0f, 50f),
            new Vector2(690f, 48f),
            TextAnchor.MiddleLeft
        );
        Text ownTimeText = CreateText(
            "OwnRunTimeText",
            opponentCard.rectTransform,
            "TIME  00:42.37",
            29,
            Green,
            new Vector2(-190f, -4f),
            new Vector2(310f, 55f),
            TextAnchor.MiddleLeft
        );
        Text ownMovesText = CreateText(
            "OwnRunMovesText",
            opponentCard.rectTransform,
            "MOVES  31 / MIN 24",
            29,
            Green,
            new Vector2(155f, -4f),
            new Vector2(380f, 55f),
            TextAnchor.MiddleLeft
        );
        Text opponentFeedbackText = CreateText(
            "OpponentFeedbackText",
            opponentCard.rectTransform,
            "OPPONENT'S MESSAGE:  --",
            29,
            Orange,
            new Vector2(0f, -113f),
            new Vector2(690f, 145f),
            TextAnchor.UpperLeft
        );
        ConfigureFeedbackText(opponentFeedbackText);

        Button backButton = CreateButton(
            "BackToLobbyButton",
            root,
            "BackToLobbyButtonText",
            "CONTINUE",
            Green,
            new Vector2(0f, -360f),
            new Vector2(430f, 78f),
            27
        );

        GameObject controllerObject = new GameObject(
            "MatchResultController",
            typeof(OnlineMatchClient),
            typeof(MatchResultController)
        );
        MatchResultController controller =
            controllerObject.GetComponent<MatchResultController>();
        SerializedObject serializedController = new SerializedObject(controller);
        SetReference(serializedController, "roomCodeText", roomCodeText);
        SetReference(serializedController, "statusText", statusText);
        SetReference(
            serializedController,
            "ownChallengeAssistantText",
            ownAssistantText
        );
        SetReference(serializedController, "ownFeedbackText", ownFeedbackText);
        SetReference(
            serializedController,
            "opponentRunTimeText",
            opponentTimeText
        );
        SetReference(
            serializedController,
            "opponentRunMovesText",
            opponentMovesText
        );
        SetReference(
            serializedController,
            "opponentChallengeAssistantText",
            opponentAssistantText
        );
        SetReference(
            serializedController,
            "opponentFeedbackText",
            opponentFeedbackText
        );
        SetReference(serializedController, "ownRunTimeText", ownTimeText);
        SetReference(serializedController, "ownRunMovesText", ownMovesText);
        SetReference(serializedController, "backToLobbyButton", backButton);
        serializedController.ApplyModifiedPropertiesWithoutUndo();

        new GameObject(
            "EventSystem",
            typeof(EventSystem),
            typeof(StandaloneInputModule)
        );

        EditorSceneManager.SaveScene(scene);
        RestoreScene(previousActiveScene, scene, openedForBuild);
    }

    private static void ConfigureOnlineLevelPlayer()
    {
        Scene previousActiveScene = SceneManager.GetActiveScene();
        Scene scene = SceneManager.GetSceneByPath(OnlineLevelScenePath);
        bool openedForBuild = !scene.IsValid() || !scene.isLoaded;

        if (openedForBuild)
        {
            scene = EditorSceneManager.OpenScene(
                OnlineLevelScenePath,
                OpenSceneMode.Additive
            );
        }

        GameObject player2Prefab = AssetDatabase.LoadAssetAtPath<GameObject>(
            "Assets/Prefebs/Player2.prefab"
        );
        LevelLoader loader = FindComponentInScene<LevelLoader>(scene);

        if (loader == null || player2Prefab == null)
        {
            throw new System.InvalidOperationException(
                "Online_Level LevelLoader or Player2 prefab is missing."
            );
        }

        SerializedObject serializedLoader = new SerializedObject(loader);
        serializedLoader.FindProperty("playerPrefab").objectReferenceValue =
            player2Prefab;
        serializedLoader.ApplyModifiedPropertiesWithoutUndo();
        PrefabUtility.RecordPrefabInstancePropertyModifications(loader);
        Text completeHint = FindNamedComponentInScene<Text>(
            scene,
            "CompleteHint"
        );

        if (completeHint != null)
        {
            completeHint.text = "SUBMITTING RESULT...";
        }

        OnlineLevelController controller =
            FindComponentInScene<OnlineLevelController>(scene);

        if (controller != null && completeHint != null)
        {
            SerializedObject serializedController =
                new SerializedObject(controller);
            SetReference(
                serializedController,
                "completeHintText",
                completeHint
            );
            serializedController.ApplyModifiedPropertiesWithoutUndo();
        }

        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        RestoreScene(previousActiveScene, scene, openedForBuild);
    }

    private static void EnsureBuildSettings()
    {
        List<EditorBuildSettingsScene> scenes =
            new List<EditorBuildSettingsScene>(EditorBuildSettings.scenes);
        AddOrEnableScene(scenes, ResultScenePath);
        AddOrEnableScene(scenes, QuestionnaireScenePath);
        EditorBuildSettings.scenes = scenes.ToArray();
    }

    private static void AddOrEnableScene(
        List<EditorBuildSettingsScene> scenes,
        string scenePath)
    {
        for (int i = 0; i < scenes.Count; i++)
        {
            if (scenes[i].path == scenePath)
            {
                scenes[i] = new EditorBuildSettingsScene(
                    scenePath,
                    true
                );
                return;
            }
        }

        scenes.Add(new EditorBuildSettingsScene(scenePath, true));
    }

    private static Canvas CreateCanvas(string name)
    {
        GameObject canvasObject = new GameObject(
            name,
            typeof(RectTransform),
            typeof(Canvas),
            typeof(CanvasScaler),
            typeof(GraphicRaycaster)
        );
        Canvas canvas = canvasObject.GetComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.matchWidthOrHeight = 0.5f;
        return canvas;
    }

    private static RectTransform CreateRect(
        string name,
        Transform parent,
        Vector2 anchoredPosition,
        Vector2 size)
    {
        GameObject gameObject = new GameObject(name, typeof(RectTransform));
        RectTransform rect = gameObject.GetComponent<RectTransform>();
        rect.SetParent(parent, false);
        rect.anchorMin = new Vector2(0.5f, 0.5f);
        rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.anchoredPosition = anchoredPosition;
        rect.sizeDelta = size;
        return rect;
    }

    private static void Stretch(RectTransform rect)
    {
        rect.anchorMin = Vector2.zero;
        rect.anchorMax = Vector2.one;
        rect.offsetMin = Vector2.zero;
        rect.offsetMax = Vector2.zero;
    }

    private static Image CreatePanel(
        string name,
        Transform parent,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size)
    {
        RectTransform rect = CreateRect(
            name,
            parent,
            anchoredPosition,
            size
        );
        Image image = rect.gameObject.AddComponent<Image>();
        image.color = color;
        image.raycastTarget = false;
        return image;
    }

    private static Text CreateText(
        string name,
        Transform parent,
        string value,
        int fontSize,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size,
        TextAnchor alignment)
    {
        RectTransform rect = CreateRect(
            name,
            parent,
            anchoredPosition,
            size
        );
        Text text = rect.gameObject.AddComponent<Text>();
        text.font = pixelFont;
        text.fontSize = fontSize;
        text.fontStyle = FontStyle.Normal;
        text.alignment = alignment;
        text.color = color;
        text.text = value;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Overflow;
        text.raycastTarget = false;
        return text;
    }

    private static void ConfigureResultCardTextLayout(
        Text timeText,
        Text movesText,
        Text feedbackText)
    {
        if (timeText == null || movesText == null || feedbackText == null)
        {
            throw new System.InvalidOperationException(
                "Match_Result is missing a result-card text element."
            );
        }

        timeText.rectTransform.anchoredPosition = new Vector2(-190f, -4f);
        timeText.rectTransform.sizeDelta = new Vector2(310f, 55f);
        movesText.rectTransform.anchoredPosition = new Vector2(155f, -4f);
        movesText.rectTransform.sizeDelta = new Vector2(380f, 55f);
        feedbackText.rectTransform.anchoredPosition = new Vector2(0f, -113f);
        feedbackText.rectTransform.sizeDelta = new Vector2(690f, 145f);
        ConfigureFeedbackText(feedbackText);
    }

    private static void ConfigureFeedbackText(Text feedbackText)
    {
        feedbackText.alignment = TextAnchor.UpperLeft;
        feedbackText.horizontalOverflow = HorizontalWrapMode.Wrap;
        feedbackText.verticalOverflow = VerticalWrapMode.Truncate;
        feedbackText.resizeTextForBestFit = true;
        feedbackText.resizeTextMinSize = 18;
        feedbackText.resizeTextMaxSize = Mathf.Max(18, feedbackText.fontSize);
    }

    private static Button CreateButton(
        string name,
        Transform parent,
        string textName,
        string label,
        Color color,
        Vector2 anchoredPosition,
        Vector2 size,
        int fontSize)
    {
        Image image = CreatePanel(
            name,
            parent,
            color,
            anchoredPosition,
            size
        );
        image.raycastTarget = true;
        Button button = image.gameObject.AddComponent<Button>();
        button.targetGraphic = image;
        ColorBlock colors = button.colors;
        colors.normalColor = Color.white;
        colors.highlightedColor = new Color(1f, 1f, 1f, 0.85f);
        colors.pressedColor = new Color(0.78f, 0.78f, 0.78f, 1f);
        colors.disabledColor = new Color(0.5f, 0.5f, 0.5f, 0.72f);
        button.colors = colors;
        Text text = CreateText(
            textName,
            image.rectTransform,
            label,
            fontSize,
            Color.white,
            Vector2.zero,
            size,
            TextAnchor.MiddleCenter
        );
        Stretch(text.rectTransform);
        return button;
    }

    private static void SetReference(
        SerializedObject serializedObject,
        string propertyName,
        Object value)
    {
        SerializedProperty property =
            serializedObject.FindProperty(propertyName);

        if (property != null)
        {
            property.objectReferenceValue = value;
        }
    }

    private static T FindComponentInScene<T>(Scene scene)
        where T : Component
    {
        GameObject[] roots = scene.GetRootGameObjects();

        for (int i = 0; i < roots.Length; i++)
        {
            T component = roots[i].GetComponentInChildren<T>(true);

            if (component != null)
            {
                return component;
            }
        }

        return null;
    }

    private static T FindNamedComponentInScene<T>(
        Scene scene,
        string objectName)
        where T : Component
    {
        GameObject[] roots = scene.GetRootGameObjects();

        for (int i = 0; i < roots.Length; i++)
        {
            Transform[] transforms =
                roots[i].GetComponentsInChildren<Transform>(true);

            for (int j = 0; j < transforms.Length; j++)
            {
                if (transforms[j].name == objectName)
                {
                    return transforms[j].GetComponent<T>();
                }
            }
        }

        return null;
    }

    private static void DestroyRootIfPresent(Scene scene, string name)
    {
        GameObject[] roots = scene.GetRootGameObjects();

        for (int i = 0; i < roots.Length; i++)
        {
            if (roots[i].name == name)
            {
                Object.DestroyImmediate(roots[i]);
                return;
            }
        }
    }

    private static void RestoreScene(
        Scene previousActiveScene,
        Scene builtScene,
        bool closeBuiltScene)
    {
        if (previousActiveScene.IsValid() && previousActiveScene.isLoaded)
        {
            SceneManager.SetActiveScene(previousActiveScene);
        }

        if (closeBuiltScene)
        {
            EditorSceneManager.CloseScene(builtScene, true);
        }
    }
}
#endif
