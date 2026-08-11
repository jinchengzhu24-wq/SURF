#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class BuildCoCreationStagePlayUi
{
    private const string PcScenePath =
        "Assets/Scenes/Matchmaking/PC_Level.unity";
    private const string DgScenePath =
        "Assets/Scenes/Matchmaking/DG_Level.unity";
    private const string FontPath =
        "Assets/Font/HarmonyOS_Sans_SC_Regular.ttf";

    [MenuItem("Tools/Co-Creation/Install Static Stage Play UI")]
    public static void Build()
    {
        string previousScenePath = SceneManager.GetActiveScene().path;
        Font font = AssetDatabase.LoadAssetAtPath<Font>(FontPath);

        if (font == null)
        {
            throw new System.InvalidOperationException(
                "The bilingual Stage Play font could not be loaded."
            );
        }

        InstallIntoScene(PcScenePath, font);
        InstallIntoScene(DgScenePath, font);

        if (!string.IsNullOrWhiteSpace(previousScenePath))
        {
            EditorSceneManager.OpenScene(previousScenePath, OpenSceneMode.Single);
        }

        AssetDatabase.SaveAssets();
        Debug.Log("Static Stage Play UI installed in PC_Level and DG_Level.");
    }

    private static void InstallIntoScene(string scenePath, Font font)
    {
        Scene scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
        GameObject existing = GameObject.Find("CoCreationPlayOverlay");

        if (existing != null)
        {
            Object.DestroyImmediate(existing);
        }

        GameObject canvasObject = new GameObject(
            "CoCreationPlayOverlay",
            typeof(RectTransform),
            typeof(Canvas),
            typeof(CanvasScaler),
            typeof(GraphicRaycaster),
            typeof(CoCreationStagePlayView)
        );
        canvasObject.layer = 5;

        Canvas canvas = canvasObject.GetComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        canvas.overrideSorting = true;
        canvas.sortingOrder = 32000;

        CanvasScaler scaler = canvasObject.GetComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.screenMatchMode = CanvasScaler.ScreenMatchMode.MatchWidthOrHeight;
        scaler.matchWidthOrHeight = 0.5f;

        Button returnButton = CreateReturnButton(canvasObject.transform, font);
        Text returnButtonText = returnButton.GetComponentInChildren<Text>();
        GameObject statusPanel = CreateStatusPanel(canvasObject.transform, font);
        Text statusText = statusPanel.GetComponentInChildren<Text>(true);

        CoCreationStagePlayView view =
            canvasObject.GetComponent<CoCreationStagePlayView>();
        SerializedObject serializedView = new SerializedObject(view);
        serializedView.FindProperty("returnButton").objectReferenceValue =
            returnButton;
        serializedView.FindProperty("returnButtonText").objectReferenceValue =
            returnButtonText;
        serializedView.FindProperty("statusPanel").objectReferenceValue =
            statusPanel;
        serializedView.FindProperty("statusText").objectReferenceValue =
            statusText;
        serializedView.ApplyModifiedPropertiesWithoutUndo();

        EnsureEventSystem();
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene, scenePath);
    }

    private static Button CreateReturnButton(Transform parent, Font font)
    {
        GameObject buttonObject = new GameObject(
            "ReturnToCoCreationLabButton",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Image),
            typeof(Button),
            typeof(Outline)
        );
        buttonObject.layer = 5;
        buttonObject.transform.SetParent(parent, false);

        RectTransform buttonRect = buttonObject.GetComponent<RectTransform>();
        buttonRect.anchorMin = new Vector2(0.5f, 1f);
        buttonRect.anchorMax = new Vector2(0.5f, 1f);
        buttonRect.pivot = new Vector2(0.5f, 1f);
        buttonRect.anchoredPosition = new Vector2(0f, -22f);
        buttonRect.sizeDelta = new Vector2(620f, 66f);

        Image buttonImage = buttonObject.GetComponent<Image>();
        buttonImage.color = new Color32(64, 122, 73, 255);

        Outline outline = buttonObject.GetComponent<Outline>();
        outline.effectColor = new Color32(35, 55, 38, 255);
        outline.effectDistance = new Vector2(4f, -4f);
        outline.useGraphicAlpha = true;

        Button button = buttonObject.GetComponent<Button>();
        button.targetGraphic = buttonImage;
        button.transition = Selectable.Transition.ColorTint;
        ColorBlock colors = button.colors;
        colors.normalColor = Color.white;
        colors.highlightedColor = new Color32(190, 226, 158, 255);
        colors.pressedColor = new Color32(145, 190, 116, 255);
        colors.selectedColor = colors.highlightedColor;
        colors.disabledColor = new Color32(150, 155, 145, 210);
        colors.colorMultiplier = 1f;
        button.colors = colors;

        Text label = CreateText(
            "Label",
            buttonObject.transform,
            font,
            "RETURN TO LAB / 返回共创工作台",
            31,
            Color.white
        );
        RectTransform labelRect = label.rectTransform;
        labelRect.anchorMin = Vector2.zero;
        labelRect.anchorMax = Vector2.one;
        labelRect.offsetMin = new Vector2(14f, 5f);
        labelRect.offsetMax = new Vector2(-14f, -5f);
        return button;
    }

    private static GameObject CreateStatusPanel(Transform parent, Font font)
    {
        GameObject panel = new GameObject(
            "CoCreationPlayStatus",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Image)
        );
        panel.layer = 5;
        panel.transform.SetParent(parent, false);

        RectTransform panelRect = panel.GetComponent<RectTransform>();
        panelRect.anchorMin = new Vector2(0.5f, 1f);
        panelRect.anchorMax = new Vector2(0.5f, 1f);
        panelRect.pivot = new Vector2(0.5f, 1f);
        panelRect.anchoredPosition = new Vector2(0f, -98f);
        panelRect.sizeDelta = new Vector2(840f, 58f);
        panel.GetComponent<Image>().color = new Color32(238, 226, 196, 242);

        Text message = CreateText(
            "StatusText",
            panel.transform,
            font,
            "PLAY STATUS",
            24,
            new Color32(47, 48, 42, 255)
        );
        RectTransform messageRect = message.rectTransform;
        messageRect.anchorMin = Vector2.zero;
        messageRect.anchorMax = Vector2.one;
        messageRect.offsetMin = new Vector2(12f, 4f);
        messageRect.offsetMax = new Vector2(-12f, -4f);
        panel.SetActive(false);
        return panel;
    }

    private static Text CreateText(
        string objectName,
        Transform parent,
        Font font,
        string content,
        int fontSize,
        Color color
    )
    {
        GameObject textObject = new GameObject(
            objectName,
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Text)
        );
        textObject.layer = 5;
        textObject.transform.SetParent(parent, false);

        Text text = textObject.GetComponent<Text>();
        text.font = font;
        text.text = content;
        text.fontSize = fontSize;
        text.fontStyle = FontStyle.Bold;
        text.alignment = TextAnchor.MiddleCenter;
        text.color = color;
        text.raycastTarget = false;
        text.resizeTextForBestFit = true;
        text.resizeTextMinSize = 18;
        text.resizeTextMaxSize = fontSize;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        return text;
    }

    private static void EnsureEventSystem()
    {
        if (Object.FindObjectOfType<EventSystem>(true) != null)
        {
            return;
        }

        new GameObject(
            "CoCreationPlayEventSystem",
            typeof(EventSystem),
            typeof(StandaloneInputModule)
        );
    }
}
#endif
