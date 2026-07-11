using System;
using TMPro;
using UnityEngine.UI;

public class ImeSafeTMPInputField : TMP_InputField
{
    public override void Rebuild(CanvasUpdate update)
    {
        try
        {
            base.Rebuild(update);
        }
        catch (IndexOutOfRangeException)
        {
            // TMP 3.0.7 can briefly produce a caret index that does not match
            // textInfo while a Windows IME composition is being committed.
            // Skipping that caret mesh rebuild lets the next valid frame recover.
        }
    }
}
