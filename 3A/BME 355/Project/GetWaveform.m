function [waveform] = GetWaveform(t, start, stop, type, amp, freq, duty, phase)
    % Input Parameters
    %   t:      time in seconds during the gait cycle
    %   start:  start time of swing phase
    %   end:    end time of swing phase
    %   type:   type of waveform (sine, square, trap)
    %   amp:    amplitude - all waves
    %   freq:   frequency (Hz) - all waves
    %   duty:   duty cycle (int from 0 to 100) - square and trap waves
    % Output
    %   waveform: FES wave from t = 0s to 1.2s
    
    waveform = zeros(1, size(t, 2));
    for i = 1:size(t,2)
        if t(i) < start || t(i) > stop
            waveform(i) = 0;
        else
            if strcmp(type, 'sine') 
                t(i) = t(i) - phase;  % Phase shift to the right
                waveform(i) = amp/2 * sin(freq*(t(i)-start-freq/0.75)) + amp/2;
            elseif strcmp(type, 'square') 
                t(i) = t(i) - phase;
                waveform(i) = amp/2 * square(freq*(t(i)-start), duty) + amp/2;
            elseif strcmp(type, 'trap') 
                margin = (stop - start)*(duty/100)/2;
                if t(i) < start + margin
                    waveform(i) = (amp/margin)*t(i) - start*amp/margin;
                elseif t(i) > stop - margin
                    waveform(i) = -(amp/margin)*t(i) + amp/margin*stop;
                else
                    waveform(i) = amp;
                end
            elseif strcmp(type, 'constant') 
                waveform(i) = amp;
            end
        end
    end
end