package fun.acat.phonerelay;

import android.content.Context;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** App 原生控件的间距、卡片与明暗配色。 */
final class RelayUi {
    final Context context;
    final boolean dark;
    final int background, panel, field, text, muted, border, accent, tint, green;

    RelayUi(Context context) {
        this.context = context;
        dark = (context.getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK)
            == Configuration.UI_MODE_NIGHT_YES;
        background = color(dark ? "#10151E" : "#F3F6FB");
        panel = color(dark ? "#1A2230" : "#FFFFFF");
        field = color(dark ? "#121B28" : "#F7F9FC");
        text = color(dark ? "#EDF2FA" : "#18263C");
        muted = color(dark ? "#A7B4C8" : "#62738B");
        border = color(dark ? "#303D51" : "#DFE6F0");
        accent = color(dark ? "#85B4FF" : "#2866CE");
        tint = color(dark ? "#243A5A" : "#EAF1FF");
        green = color(dark ? "#7BDBB0" : "#137856");
    }
    static int color(String value) { return Color.parseColor(value); }
    int dp(float value) { return Math.round(value * context.getResources().getDisplayMetrics().density); }
    GradientDrawable rounded(int fill, int stroke, int radius) {
        GradientDrawable shape = new GradientDrawable(); shape.setColor(fill);
        shape.setCornerRadius(dp(radius)); if (stroke != 0) shape.setStroke(dp(1), stroke);
        return shape;
    }
    LinearLayout vertical() {
        LinearLayout box = new LinearLayout(context); box.setOrientation(LinearLayout.VERTICAL); return box;
    }
    TextView text(String value, int size, int foreground, boolean bold) {
        TextView view = new TextView(context); view.setText(value); view.setTextSize(size);
        view.setTextColor(foreground); view.setLineSpacing(dp(3), 1);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return view;
    }
    void add(LinearLayout parent, View view, int bottom) {
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(-1, -2);
        layout.bottomMargin = dp(bottom); parent.addView(view, layout);
    }
    LinearLayout card(LinearLayout parent, String title, String description) {
        LinearLayout box = vertical(); box.setPadding(dp(18), dp(18), dp(18), dp(6));
        box.setBackground(rounded(panel, border, 18));
        add(box, text(title, 17, text, true), description.isEmpty() ? 16 : 6);
        if (!description.isEmpty()) add(box, text(description, 13, muted, false), 18);
        add(parent, box, 16); return box;
    }
    EditText field(LinearLayout parent, String label, String hint, String value, boolean secret, boolean number) {
        add(parent, text(label, 12, muted, true), 7);
        EditText edit = new EditText(context); edit.setTextSize(15); edit.setTextColor(text);
        edit.setHintTextColor(muted); edit.setHint(hint); edit.setSingleLine(true);
        edit.setInputType(number
            ? InputType.TYPE_CLASS_NUMBER | (secret ? InputType.TYPE_NUMBER_VARIATION_PASSWORD : 0)
            : InputType.TYPE_CLASS_TEXT | (secret ? InputType.TYPE_TEXT_VARIATION_PASSWORD : InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS));
        edit.setTypeface(Typeface.DEFAULT); edit.setText(value); edit.setSelectAllOnFocus(true);
        edit.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        edit.setPadding(dp(13), dp(12), dp(13), dp(12)); edit.setMinHeight(dp(48));
        edit.setBackground(rounded(field, border, 10));
        add(parent, edit, 16); return edit;
    }
    Button button(LinearLayout parent, String label, boolean primary, Runnable action) {
        Button button = new Button(context); button.setText(label); button.setAllCaps(false);
        button.setTextSize(14); button.setMinHeight(dp(48)); button.setMinimumHeight(dp(48));
        button.setTextColor(primary ? (dark ? color("#122038") : Color.WHITE) : accent);
        button.setBackgroundTintList(null);
        button.setBackground(rounded(primary ? accent : field, primary ? 0 : border, 10));
        button.setGravity(Gravity.CENTER); button.setPadding(dp(12), dp(10), dp(12), dp(10));
        button.setOnClickListener(v -> action.run()); add(parent, button, 12); return button;
    }
}
