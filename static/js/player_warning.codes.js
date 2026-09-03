(function($) {
    $(document).ready(function() {
        function checkPlayerWarnings() {
            let playerId = $('#id_player').val();
            let gameweekId = $('#id_gameweek').val();

            if (playerId && gameweekId) {
                fetch(`/api/get-player-previous-yellows/?player_id=${playerId}&gameweek_id=${gameweekId}`)
                    .then(response => response.json())
                    .then(data => {
                        $('#yellow-warning-banner').remove();
                        if (data.has_yellow_before) {
                            let warningHtml = `
                                <div id="yellow-warning-banner" class="alert alert-warning" style="background: #fffaee; border-left: 4px solid #ffbf00; padding: 10px; margin: 10px 0; color: #735100; font-weight: bold;">
                                    ⚠️ تنبيه إيقاف: هذا اللاعب لديه (${data.previous_yellows}) كارت أصفر في الجولات السابقة! الإنذار الحالي سيعرضه للإيقاف.
                                </div>`;
                            // وضع التنبيه أعلى حقل اللاعب مباشرة
                            $('.field-player').first().prepend(warningHtml);
                        }
                    }).catch(err => console.error("Error fetching player warnings:", err));
            }
        }

        // مراقبة التغيير عند اختيار اللاعب أو تغيير الجولة
        $(document).on('change blur', '#id_player, #id_gameweek', function() {
            checkPlayerWarnings();
        });

        // فحص تلقائي إذا كان اللاعب محدداً مسبقاً عند فتح صفحة التعديل
        if ($('#id_player').val()) {
            checkPlayerWarnings();
        }
    });
})(django.jQuery);