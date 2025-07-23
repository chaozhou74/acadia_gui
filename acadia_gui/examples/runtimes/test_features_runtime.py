from typing import Union, Literal, Annotated

import numpy as np

from acadia import Acadia, DataManager
from acadia.runtime import annotate_method
from acadia_qmsmt import QMsmtRuntime, MeasurableResonator, IOConfig


class ResonatorSpectroscopyTestGuiRuntime(QMsmtRuntime):
    """
    Minimal‑yet‑functional resonator‑spectroscopy runtime to showcase **acadia_gui**.

    We borrow the core pulse sequence from a resonator‑spectroscopy experiment so that
    the GUI always has something to plot.  For context, skim `README.md` first.


    The processing, plotting, and button logic in this runtime is intentionally a bit over-complicated.
    It's designed to demonstrate everything the GUI can do — and how to do it — while showcasing helper
    functions from acadia_qmsmt that could help simplify common plotting tasks.

    When writing your own runtime, the examples in `acadia_qmsmt.runtimes` are better references,
    as they follow more practical and streamlined patterns.

    """

    # YAML section names for the two IO channels required by this runtime
    stimulus: IOConfig
    capture: IOConfig

    # Sweep parameters 
    frequencies: Union[list, np.ndarray]
    iterations: int   # ******** IMPORTANT: This attribute is expected by the GUI for calculating progress bar ETA ********

    # Misc. runtime knobs
    capture_window_name: str  = None
    stimulus_waveform_name: str = None
    electrical_delay: float = 0.0  # s; positive means phase lags with frequency

    run_delay: int  # ns of pad‑time between shots
    figsize: tuple[int] = None
    yaml_path: str = None

    # Example parameter for dynamically enable/disable a button
    disable_button5: bool = True

    # =====================================================================
    #                Runtime experiment sequence definition
    # =====================================================================
    def main(self):
        import logging
        logger = logging.getLogger("acadia")

        stimulus_io = self.io("stimulus")
        capture_io = self.io("capture")

        resonator = MeasurableResonator(stimulus_io, capture_io)

        # Create the record group for saving captured data
        self.data.add_group(f"points", uniform=True)

        # Create a sequence for the sequencer to generate the pulse and capture it
        def sequence(a: Acadia):
            with a.channel_synchronizer():
                # Measure the resonator by driving the "readout" waveform on the stimulus IO
                # and capture into the "readout_accumulated" waveform on the capture IO
                resonator.measure("readout", "readout_accumulated", self.capture_window_name)

        # misc hardware/software preparations
        self.acadia.compile(sequence)
        self.acadia.attach()
        self.configure_channels()
        self.acadia.assemble()
        self.acadia.load()

        # Load the window memory with the data from the config file
        resonator.load_windows()
        # Load the stimulus waveform named "readout" with the specified signal
        stimulus_io.load_waveform("readout", self.stimulus_waveform_name)

        for i in range(self.iterations):
            for frequency in self.frequencies:
                resonator.set_frequency(frequency)

                # capture data and put in the corresponding group
                self.acadia.run(minimum_delay=self.run_delay)

                wf = capture_io.get_waveform_memory("readout_accumulated")
                self.data[f"points"].write(wf.array)

            if self.data.serve() == DataManager.serve_hangup():
                self.data.disconnect()
                return

        self.final_serve()


    # =====================================================================================
    #   Standard `initialize`, `update`, and `finalize` functions expected by QMsmtRuntime
    # =====================================================================================
    def initialize(self):
        pass

    def update(self):
        # save current data
        self.data.save(self.local_directory)

    def finalize(self):
        super().finalize()
        # run all the plot methods with tag `plot_name=...` and save the generated plots
        from acadia_qmsmt.plotting import save_registered_plots
        save_registered_plots(self)



    # =====================================================================
    #                            DATA PROCESSOR
    # =====================================================================
    # The data processor method must be annotated with `is_data_processor=True`.
    # It gets called on every update (poll) in the GUI.
    #
    # The job of this method is to prepare all the data needed by the plotter methods,
    # and store those results as class attributes. That way, the plotters don't need
    # to take any explicit inputs — they just use what's already stored.
    #
    # This method can take arbitrary **keyword** arguments, which will show up
    # as input fields in the GUI. Two special typehints are recognized:
    # - `bool` or `Bool`: shown as a checkbox
    # - `Literal[...]`: shown as a dropdown menu with options
    # - `Annotated[type, "key_word", metadata]`: Allows for custom Qt Objects (key_word) to be added to the layout. 
    @annotate_method(is_data_processor=True)
    def process_current_data(self, e_delay: float = 76e-9, auto_edelay: bool = False):
        """
        Re-process all data each time this method is called. (For now, we do full reprocessing
        every time, but this could be optimized later to just update with new data.)

        :param e_delay:
        :param auto_edelay:
        :return:
        """

        # ------ gather the data ----------------------------
        # Reshape the raw IQ data into a convenient array format using a helper
        from acadia_qmsmt.analysis import reshape_iq_data_by_axes
        data = reshape_iq_data_by_axes(self.data["points"].records(), self.frequencies)
        # `data` is now shape (iterations, len(frequencies), 2), or None if no data yet.
        # the last axis is for real and imaginary data
        if data is None:
            return
        self.completed_iterations = len(data)

        # Convert to complex array and compute stats
        self.data_iq = data.astype(float).view(complex).squeeze()
        self.avg_iq = np.mean(self.data_iq, axis=0)
        self.err_iq = np.std(self.data_iq, axis=0) / np.sqrt(self.completed_iterations)

        # --------- process: phase correction -------------------
        # Optionally fit for e_delay (based on the input `auto_edelay`)
        # apply e_delay and assign the result to `self.avg_iq_corrected`
        if auto_edelay:
            from numpy import polyfit
            k, _ = polyfit(self.frequencies, np.unwrap(np.angle(self.avg_iq)), deg=1)
            e_delay = -k / (2 * np.pi)
        self.avg_iq_corrected = self.avg_iq * np.exp(1j * self.frequencies * e_delay * 2 * np.pi)
        self.e_delay_applied = e_delay

        # --------- process: fit amplitude to Lorentzian -------------------
        from acadia_qmsmt.analysis.fitting.lorentzian import Lorentzian
        # acadia_qmsmt includes a few FitterBase subclasses as wrappers for scipy.curve_fit,
        # they provide some shortcuts to plotting, generating ufloat results, etc.
        # You can use them, or ignore them and roll your own with your favourite fitting packcage.
        self.fit = Lorentzian(self.frequencies, np.abs(self.avg_iq), sigma=self.err_iq)
        self.fitted_f0 = self.fit.ufloat_results["x0"]

        # *** IMPORTANT: the gui is expecting this function to return the currently completed iterations to construct
        # the progress bar.  ***
        # We are trying to minimize the expected class attribute names that the GUI expects,
        # so we didn't make it just look for rt.completed_iterations.
        return self.completed_iterations


    # ==================================================================================================
    #                            PLOTTERS (EXAMPLES)
    # ==================================================================================================
    # Each plot method must be annotated with `plot_name="..."` for the GUI to find it.
    #
    # There are two supported styles for GUI-driven layout generation, and each one
    # expects different kwargs in the plot method:
    #
    #   1. Axes-based (`axs` kwarg): GUI pre-generates subplots using `axs_shape` and
    #      passes them to the method. You must specify `axs_shape=(rows, cols)` in the
    #      decorator, unless it's just a single subplot (then it defaults to (1, 1)).
    #
    #      This approach is faster, since the GUI keeps the same axes and just clears/redraws.
    #
    #   2. Figure-based (`fig` kwarg): GUI hands over a blank figure, and the method is
    #      responsible for creating all layout (e.g., via `add_subplot`, `add_gridspec`, etc.).
    #      This gives more flexibility to the user, but the GUI will redraw the whole layout each time.
    #
    # We'll first show some dummy examples to demonstrate how both modes work.
    # After that, we'll build actual plots using helpers provided in acadia_qmsmt, which
    # could help simplify writing commonly used plotting methods.

    # --- Dummy example #1: axes‑based layout ----------------------------
    @annotate_method(plot_name="test_1_ax_based", axs_shape=(2, 1))
    def plot_test1(self, axs=None, ax1_label=1, hide_ax2: bool = False,
                   ax2_color: Literal["r", "g", "b"] = "g"):
        """
        Similar to the data processor method, the plotter method can also take kwargs, and GUI will display them, same
        typehint rules also apply.
        """
        import matplotlib.pyplot as plt
        if axs is None:
            fig, axs = plt.subplots(2, 1) # For using the same plotter outside the gui, create new plot axes if not provided
        else:
            fig = axs[0].get_figure() # If using gui, axs will be passed in, we just grab the figure here for consistency

        axs[0].plot(self.frequencies, self.avg_iq.real, label=f"{ax1_label}")
        if not hide_ax2: # a test bool kwarg just to show we can make dynamic changes
            axs[1].plot(self.frequencies, self.avg_iq.imag, color=f"{ax2_color}", label="imag")

        for ax in axs:
            ax.legend()

        # *** IMPORTANT: the `save_registered_plots` is expecting this function to return the fig and axs to do the saving ***
        return fig, axs

    # --- Dummy example #2: figure‑based layout --------------------------
    @annotate_method(plot_name="test_2_fig_based")
    def plot_test2(self, fig=None, ax1_label=1, ax2_color: Literal["r", "g", "b"] = "g"):
        """Now we own the `Figure` and we can lay it out however we like."""
        from matplotlib.pyplot import figure
        fig = figure() if fig is None else fig

        # make a layout with to rows on the left and one plot on the right
        gs = fig.add_gridspec(2, 2)
        ax_tl = fig.add_subplot(gs[0, 0])
        ax_bl = fig.add_subplot(gs[1, 0])
        ax_r  = fig.add_subplot(gs[:, 1])
        axs = [ax_tl, ax_bl, ax_r]

        ax_tl.plot(self.frequencies, self.avg_iq.real, label=str(ax1_label))
        ax_bl.plot(self.frequencies, self.avg_iq.imag, color=ax2_color)
        ax_r.plot(self.avg_iq.real, self.avg_iq.imag)
        for ax in axs:
            ax.legend()

        return fig, axs
    
    @annotate_method(plot_name="test_2_fig_based")
    def plot_test2(self, fig=None, ax1_label=1, ax2_color: Literal["r", "g", "b"] = "g"):
        """Now we own the `Figure` and we can lay it out however we like."""
        from matplotlib.pyplot import figure
        fig = figure() if fig is None else fig

        # make a layout with to rows on the left and one plot on the right
        gs = fig.add_gridspec(2, 2)
        ax_tl = fig.add_subplot(gs[0, 0])
        ax_bl = fig.add_subplot(gs[1, 0])
        ax_r  = fig.add_subplot(gs[:, 1])
        axs = [ax_tl, ax_bl, ax_r]

        ax_tl.plot(self.frequencies, self.avg_iq.real, label=str(ax1_label))
        ax_bl.plot(self.frequencies, self.avg_iq.imag, color=ax2_color)
        ax_r.plot(self.avg_iq.real, self.avg_iq.imag)
        for ax in axs:
            ax.legend()

        return fig, axs
    
    # --- Dummy example 3: Using Annotated type to create a slider. Sliders are mostly helpful for 2D sweeps for taking linecuts.
    #     The slider will show up with a line editor to the left of it indicating the current value, which can also be changed in
    #     the line editor, and rounded to the closest element in the array. 

    #     Please note that currently sliders only work for float/int arrays. 
    @annotate_method(plot_name='dummy 2D sweep')
    def plot_test3(self, fig=None, apply_e_delay:bool=True, freq = Annotated[float, "slider", "self.frequencies"]=None):
        from acadia_qmsmt.plotting import prepare_plot_axes
        fig, axs = prepare_plot_axes(fig)
        # Note that freq which represents the slider value comes in as an index which makes sure the slider has linear ticks.
        # However, it is assumed that if a value is provided for the parameter it is interperted as the frequency value (a little confusing but it only matters
        # for the instantiation of the plot and you can simply manually change the value in the gui.) 
        freq = 0 if freq is None else np.argmin(np.abs(self.frequencies - freq))
        freq_idx = freq

        # Simulating a 2d sweep with random data.
        amplitudes = np.linspace(0.0, 0.9, len(self.frequencies))  # you control how many amplitude steps

        # Create 2D noise array: shape (len(frequencies), len(amplitudes))
        noise_array = np.random.normal(loc=0.0, scale=1.0, size=(len(self.frequencies), len(amplitudes)))

        # Optional: scale noise by amplitude
        # Each column gets scaled by its amplitude
        scaled_noise = noise_array * amplitudes
        axs.plot(amplitudes, scaled_noise[freq_idx,:])
        return fig, axs


    
    # --- Real plot 1: amplitude & phase vs. DAC --------------------------
    @annotate_method(plot_name="mag_phase_vs_dac", axs_shape=(2, 1))
    def plot_data(self, axs=None, apply_e_delay: bool = True, unwrap_phase: bool = True):

        # ------------ use a helper function to prepare plot axes ------------------------
        # this is a very lightweight helper
        # if axs is None, it generates new subplots with plt.subplots(*axs_shape). For use outside the gui.
        # if axs is not None, it just returns the axs and its figure. The axes will be pre-generated by the GUI
        #   and passed to this plotter, so this function just pass it through, `axs_shape` will be omitted
        from acadia_qmsmt.plotting import prepare_plot_axes
        fig, axs = prepare_plot_axes(axs, axs_shape=(2, 1), figsize=self.figsize)


        # use the plot method in the fitter to make the amplitude plot with error bar and fitted curve
        self.fit.plot(axs[0], oversample=10,
                      data_kwargs={"marker": "o"}, result_kwargs={"label":f"{self.fitted_f0}"})


        # plot phase
        #  -------- plot data w/ or w/o edelay ------------
        data = self.avg_iq_corrected if apply_e_delay else self.avg_iq
        phases = np.angle(data, deg=True)
        if unwrap_phase:
            phases = np.unwrap(phases, period=360)
        e_delay_label = None if not apply_e_delay else f"edelay: {self.e_delay_applied}"
        axs[1].plot(self.frequencies, phases, ".-", label=e_delay_label)

        # labels, etc
        axs[1].set_xlabel("Frequency [Hz]")
        axs[1].set_ylabel("Phase (deg)")
        axs[0].set_ylabel("Mag (a.u.)")

        for ax in axs:
            ax.legend()
            ax.grid(True)

        fig.tight_layout()

        return fig, axs


    # --- Real plot 2: mix‑and‑match layout + pseudo Smith chart ----------
    @annotate_method(plot_name="test arbitrary laypout", axs_shape=(2, 1))
    def plot_data_2(self, fig=None,
                    data_type: Literal["mag_phase", "re_im"] = "mag_phase",
                    apply_e_delay: bool = True):
        """Flexible playground plot to experiment with layouts."""
        from matplotlib.pyplot import figure
        fig = figure() if fig is None else fig

        # make a layout with two rows on the left and one plot on the right
        gs = fig.add_gridspec(2, 2)
        ax_tl = fig.add_subplot(gs[0, 0])
        ax_bl = fig.add_subplot(gs[1, 0])
        ax_polar = fig.add_subplot(gs[:, 1], projection="polar")
        axs = [ax_tl, ax_bl, ax_polar]

        # left column, either mag-phase or re-im
        data = self.avg_iq_corrected if apply_e_delay else self.avg_iq
        ax_tl.set_title(f"edelay: {self.e_delay_applied:.1e}s" if apply_e_delay else "raw data")

        if data_type == "mag_phase":
            ax_tl.plot(self.frequencies, np.abs(data), ".-", label="amp")
            ax_bl.plot(self.frequencies, np.angle(data), ".-", label="phase (rad)")
        else:
            ax_tl.plot(self.frequencies, data.real, ".-", label="real")
            ax_bl.plot(self.frequencies, data.imag, ".-", label="imag")
        ax_bl.set_xlabel("Frequency [Hz]")

        # right plot, fake Smith chart
        theta = np.linspace(0, 2 * np.pi, 500)
        axs[2].plot(theta, np.ones_like(theta), 'k--', lw=1)
        # Plot the S-parameter
        axs[2].plot(np.angle(data), np.abs(data)/np.max(data), 'r-')

        axs[2].set_title("Fake Smith Chart")
        axs[2].set_rticks([0.2, 0.4, 0.6, 0.8, 1.0])
        axs[2].set_rlabel_position(135)

        # labels, etc
        for ax in axs:
            ax.legend()
            ax.grid(True)

        fig.tight_layout()
        return fig, axs

    # ==================================================================================================
    #                             Example button methods
    # ==================================================================================================
    # Button methods are usually used to update parts of the config file (e.g. based on fitted results),
    # but they can also be general-purpose utilities or diagnostics.
    #
    # Each method must be decorated with `button_name="..."` so the GUI knows to show it.
    #
    # - If the function has no arguments, it appears as a compact button, with the button name.
    #   The GUI can fit up to 6 of these in one row, then move to the next row
    #
    # - If the function does take arguments, the GUI gives it an entire row
    #   and renders input fields for the kwargs based on their type hints.

    @annotate_method(button_name="update frequency")
    def update_freq(self):
        self.update_io_yaml_field("stimulus", "channel_config.nco_frequency", np.round(self.fitted_f0.n))
        self.update_io_yaml_field("capture", "channel_config.nco_frequency", np.round(self.fitted_f0.n))

    # -------- some dummy test buttons ---------------
    @annotate_method(button_name="test_1")
    def test_print1(self):
        print("test button 1")
        print(self.completed_iterations)

    @annotate_method(button_name="test_2")
    def test_print2(self):
        print("test button 2")
        print(self.completed_iterations)

    @annotate_method(button_name="test_3 this is a loooooooong name")
    def test_print3(self):
        print("test button 3")
        print(self.completed_iterations)

    # test button with various types of arguments
    @annotate_method(button_name="test_4, takes args")
    def test_print4(self, part1="This", part2:bool=True,
                    part3:Literal["Landau-Zener", "something I don't understand"]="Landau-Zener"):
        print(f"{part1} {'must be' if part2 else 'can`t be'} {part3} so it is "
              f"{'something I don`t understand' if part3 == 'Landau-Zener' else 'Landau-Zener'}")
        print(self.completed_iterations)


    # =====================================================================
    # Optional: programmatically enable/disable/create buttons or plots
    # =====================================================================
    # If defined, the method annotated with `is_customizer=True` will be executed at the beginning of
    # creating a live plotting layout
    # You can use it to hide/show/create buttons or plots based on runtime conditions or generate them in loops
    @annotate_method(is_customizer=True)
    def _customize_gui(self):

        # -------- programmatically disable/enable a button ------------
        # The GUI ignores methods with the tag `disable=True`.
        # This example disables `test_print5` based on a runtime attribute.
        # NOTE: disable must be set on the *original function object* (i.e. .__func__), not the bound method.
        # this is wrapped in this `set_method_annotation` function
        from acadia_qmsmt.helpers import set_method_annotation
        set_method_annotation(self.test_print5, disable=self.disable_button5)


        # -------- programmatically adding new plots ------------
        # make a plotter factory function
        def plot_factory(i_val):
            @annotate_method(plot_name=f"programmatically made plot {i_val}")
            def plot(axs=None, test_label="hello"):
                from acadia_qmsmt.plotting import prepare_plot_axes
                fig, axs = prepare_plot_axes(axs, axs_shape=(1, 1), figsize=self.figsize)
                axs.plot(np.array([1, 2, 3]) + i_val, label=f"{test_label}")
                axs.legend()
                return fig, axs

            return plot

        # generate plotter functions and add them to class attributes in a loop
        for i in range(2): # let's make 2 different ones just to show we don't have any late-binding issue
            setattr(self, f"plot_program_{i}", plot_factory(i))


    # This button will only show up if `disable_button5` is False
    @annotate_method(button_name="test_5, programmatically enabled")
    def test_print5(self):
        print("test button 5")
        print(self.completed_iterations)
