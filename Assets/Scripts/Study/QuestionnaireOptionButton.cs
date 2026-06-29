using UnityEngine;
using UnityEngine.UI;

public class QuestionnaireOptionButton : MonoBehaviour
{
    public int questionIndex = 1;
    public int optionIndex;
    public string questionId = "q1";
    public string optionId = "a";
    public Text questionText;
    public Text optionText;

    [Header("Selection Colors")]
    public Color normalColor = new Color(0.96f, 0.98f, 1f, 1f);
    public Color selectedColor = new Color(0.1f, 0.35f, 0.75f, 1f);
    public Color normalTextColor = new Color(0.12f, 0.18f, 0.26f, 1f);
    public Color selectedTextColor = Color.white;

    private Button button;
    private Image image;

    public Button Button
    {
        get
        {
            ResolveReferences();
            return button;
        }
    }

    public string QuestionTextValue
    {
        get
        {
            ResolveReferences();
            return questionText != null ? questionText.text : questionId;
        }
    }

    public string OptionTextValue
    {
        get
        {
            ResolveReferences();
            return optionText != null ? optionText.text : optionId;
        }
    }

    public void ResolveReferences()
    {
        if (button == null)
        {
            button = GetComponent<Button>();
        }

        if (image == null)
        {
            image = GetComponent<Image>();
        }

        if (optionText == null)
        {
            optionText = GetComponentInChildren<Text>();
        }
    }

    public void SetSelected(bool selected)
    {
        ResolveReferences();

        if (image != null)
        {
            image.color = selected ? selectedColor : normalColor;
        }

        if (optionText != null)
        {
            optionText.color = selected ? selectedTextColor : normalTextColor;
        }
    }
}
