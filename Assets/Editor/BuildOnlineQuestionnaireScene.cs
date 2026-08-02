#if UNITY_EDITOR
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class BuildOnlineQuestionnaireScene
{
    private const string ScenePath =
        "Assets/Scenes/Matchmaking/Online/Questionnaire(Online).unity";

    private static readonly Color Ink = new Color32(52, 67, 70, 255);
    private static readonly Color Cream = new Color32(255, 240, 211, 248);
    private static readonly Color Blue = new Color32(82, 104, 177, 255);
    private static readonly Color Green = new Color32(67, 155, 91, 255);
    private static readonly Color Track = new Color32(180, 168, 143, 255);
    private static readonly Color Tick = new Color32(91, 99, 92, 255);

    private static Font pixelFont;

    [MenuItem("Tools/Online/Build Online Questionnaire Scene")]
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

        Scene previousActiveScene = SceneManager.GetActiveScene();
        Scene scene = SceneManager.GetSceneByPath(ScenePath);
        bool openedForBuild = !scene.IsValid() || !scene.isLoaded;

        if (openedForBuild)
        {
            scene = EditorSceneManager.OpenScene(
                ScenePath,
                OpenSceneMode.Additive
            );
        }

        SceneManager.SetActiveScene(scene);
        DestroyRootIfPresent(scene, "QuestionnaireCanvas");
        DestroyRootIfPresent(scene, "QuestionnaireController");
        DestroyRootIfPresent(scene, "EventSystem");

        Canvas canvas = CreateCanvas("QuestionnaireCanvas");
        RectTransform root = CreateRect(
            "QuestionnaireUIRoot",
            canvas.transform,
            Vector2.zero,
            Vector2.zero
        );
        Stretch(root);

        CreateText(
            "SurveyTitleText",
            root,
            "ONLINE QUESTIONNAIRE",
            55,
            Ink,
            new Vector2(0f, 468f),
            new Vector2(1400f, 80f),
            TextAnchor.MiddleCenter
        );
        CreateText(
            "SurveySubtitleText",
            root,
            "RATE EACH ITEM FROM 1 TO 5",
            21,
            Blue,
            new Vector2(0f, 414f),
            new Vector2(1000f, 38f),
            TextAnchor.MiddleCenter
        );

        CreateScoreCard(
            root,
            "Question1",
            4,
            "q4",
            "After using this tool, how confident were you in designing a game level?",
            "NOT CONFIDENT",
            "VERY CONFIDENT",
            255f
        );
        CreateScoreCard(
            root,
            "Question2",
            5,
            "q5",
            "Did the AI suggestions inspire you or limit your thinking?",
            "LIMITED MY IDEAS",
            "INSPIRED ME",
            25f
        );
        CreateScoreCard(
            root,
            "Question3",
            6,
            "q6",
            "How much control did you feel during the level creation process?",
            "ALMOST NO CONTROL",
            "FULLY IN CONTROL",
            -205f
        );

        Button submitButton = CreateButton(
            "SubmitButton",
            root,
            "SubmitButtonText",
            "SUBMIT",
            Green,
            new Vector2(0f, -432f),
            new Vector2(360f, 72f),
            29
        );
        Text statusText = CreateText(
            "StatusText",
            root,
            "",
            19,
            Ink,
            new Vector2(0f, -493f),
            new Vector2(1400f, 42f),
            TextAnchor.MiddleCenter
        );

        GameObject controllerObject = new GameObject(
            "QuestionnaireController",
            typeof(QuestionnaireController)
        );
        QuestionnaireController controller =
            controllerObject.GetComponent<QuestionnaireController>();
        SerializedObject serializedController = new SerializedObject(controller);
        serializedController.FindProperty("surveyId").stringValue =
            "online_post_match_survey";
        serializedController.FindProperty("surveyTitle").stringValue =
            "Online Match Questionnaire";
        serializedController.FindProperty("nextSceneName").stringValue = "Menu";
        serializedController.FindProperty("requirePlayerName").boolValue = false;
        serializedController.FindProperty("startsSurveyPair").boolValue = false;
        serializedController.FindProperty("pixelFont").objectReferenceValue =
            pixelFont;
        SetReference(serializedController, "submitButton", submitButton);
        SetReference(serializedController, "statusText", statusText);
        serializedController.ApplyModifiedPropertiesWithoutUndo();

        new GameObject(
            "EventSystem",
            typeof(EventSystem),
            typeof(StandaloneInputModule)
        );

        EnsureBuildSettings();
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        AssetDatabase.SaveAssets();

        if (previousActiveScene.IsValid() && previousActiveScene.isLoaded)
        {
            SceneManager.SetActiveScene(previousActiveScene);
        }

        if (openedForBuild)
        {
            EditorSceneManager.CloseScene(scene, true);
        }

        Debug.Log("Online questionnaire scene was built successfully.");
    }

    private static void CreateScoreCard(
        Transform parent,
        string name,
        int questionIndex,
        string questionId,
        string question,
        string lowLabel,
        string highLabel,
        float y)
    {
        Image card = CreatePanel(
            name + "Card",
            parent,
            Cream,
            new Vector2(0f, y),
            new Vector2(1540f, 205f)
        );
        CreatePanel(
            name + "Accent",
            card.rectTransform,
            Blue,
            new Vector2(-757f, 0f),
            new Vector2(26f, 205f)
        );
        Text questionText = CreateText(
            name + "Text",
            card.rectTransform,
            question,
            25,
            Ink,
            new Vector2(0f, 63f),
            new Vector2(1410f, 52f),
            TextAnchor.MiddleLeft
        );
        Slider slider = CreateFivePointSlider(
            name + "Slider",
            card.rectTransform,
            new Vector2(-100f, -5f),
            new Vector2(980f, 46f)
        );
        CreateText(
            name + "LowLabel",
            card.rectTransform,
            lowLabel,
            16,
            Ink,
            new Vector2(-565f, -72f),
            new Vector2(330f, 34f),
            TextAnchor.MiddleLeft
        );
        CreateText(
            name + "HighLabel",
            card.rectTransform,
            highLabel,
            16,
            Ink,
            new Vector2(365f, -72f),
            new Vector2(330f, 34f),
            TextAnchor.MiddleRight
        );

        Image scoreBox = CreatePanel(
            name + "ScoreBox",
            card.rectTransform,
            Blue,
            new Vector2(650f, -5f),
            new Vector2(112f, 82f)
        );
        Text scoreText = CreateText(
            "ScoreValueText",
            scoreBox.rectTransform,
            "3",
            42,
            Color.white,
            Vector2.zero,
            scoreBox.rectTransform.sizeDelta,
            TextAnchor.MiddleCenter
        );
        Stretch(scoreText.rectTransform);

        QuestionnaireScoreSlider scoreComponent =
            card.gameObject.AddComponent<QuestionnaireScoreSlider>();
        SerializedObject serializedScore = new SerializedObject(scoreComponent);
        serializedScore.FindProperty("questionIndex").intValue = questionIndex;
        serializedScore.FindProperty("questionId").stringValue = questionId;
        serializedScore.FindProperty("defaultScore").intValue = 3;
        SetReference(serializedScore, "questionText", questionText);
        SetReference(serializedScore, "scoreSlider", slider);
        SetReference(serializedScore, "scoreText", scoreText);
        serializedScore.ApplyModifiedPropertiesWithoutUndo();
    }

    private static Slider CreateFivePointSlider(
        string name,
        Transform parent,
        Vector2 anchoredPosition,
        Vector2 size)
    {
        RectTransform root = CreateRect(name, parent, anchoredPosition, size);
        Slider slider = root.gameObject.AddComponent<Slider>();
        slider.minValue = 1f;
        slider.maxValue = 5f;
        slider.wholeNumbers = true;
        slider.value = 3f;
        slider.direction = Slider.Direction.LeftToRight;

        Image background = CreatePanel(
            "Track",
            root,
            Track,
            Vector2.zero,
            new Vector2(930f, 12f)
        );
        background.raycastTarget = false;

        RectTransform fillArea = CreateRect(
            "Fill Area",
            root,
            Vector2.zero,
            new Vector2(930f, 12f)
        );
        Image fill = CreatePanel(
            "Fill",
            fillArea,
            Blue,
            Vector2.zero,
            fillArea.sizeDelta
        );
        Stretch(fill.rectTransform);
        fill.raycastTarget = false;

        for (int score = 1; score <= 5; score++)
        {
            float x = -465f + ((score - 1) * 232.5f);
            CreatePanel(
                "Tick" + score,
                root,
                Tick,
                new Vector2(x, 0f),
                new Vector2(4f, 26f)
            );
            CreateText(
                "TickLabel" + score,
                root,
                score.ToString(),
                18,
                Ink,
                new Vector2(x, -37f),
                new Vector2(50f, 28f),
                TextAnchor.MiddleCenter
            );
        }

        RectTransform handleArea = CreateRect(
            "Handle Slide Area",
            root,
            Vector2.zero,
            new Vector2(930f, 46f)
        );
        Image handle = CreatePanel(
            "Handle",
            handleArea,
            Blue,
            Vector2.zero,
            new Vector2(42f, 42f)
        );
        handle.sprite = AssetDatabase.GetBuiltinExtraResource<Sprite>(
            "UI/Skin/Knob.psd"
        );
        handle.preserveAspect = true;
        handle.raycastTarget = true;

        slider.fillRect = fill.rectTransform;
        slider.handleRect = handle.rectTransform;
        slider.targetGraphic = handle;
        ColorBlock colors = slider.colors;
        colors.normalColor = Color.white;
        colors.highlightedColor = new Color(1f, 0.9f, 0.72f, 1f);
        colors.pressedColor = new Color(0.85f, 0.85f, 0.85f, 1f);
        colors.selectedColor = Color.white;
        slider.colors = colors;
        return slider;
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
        RectTransform rect = CreateRect(name, parent, anchoredPosition, size);
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
        RectTransform rect = CreateRect(name, parent, anchoredPosition, size);
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
        button.interactable = true;
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
        SerializedProperty property = serializedObject.FindProperty(propertyName);

        if (property != null)
        {
            property.objectReferenceValue = value;
        }
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

    private static void EnsureBuildSettings()
    {
        List<EditorBuildSettingsScene> scenes =
            new List<EditorBuildSettingsScene>(EditorBuildSettings.scenes);

        for (int i = 0; i < scenes.Count; i++)
        {
            if (scenes[i].path == ScenePath)
            {
                scenes[i] = new EditorBuildSettingsScene(ScenePath, true);
                EditorBuildSettings.scenes = scenes.ToArray();
                return;
            }
        }

        scenes.Add(new EditorBuildSettingsScene(ScenePath, true));
        EditorBuildSettings.scenes = scenes.ToArray();
    }
}
#endif
